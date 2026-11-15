#!/usr/bin/python3
"""One-shot startup guard for the patched GNOME Shell runtime.

The login session is the only place where the custom compositor may replace the
stock one, so that single decision is recorded durably and every ambiguous
repeat is refused. This module is not a daemon and never forks, signals, or
executes anything: each action performs exactly one transition of a small
bounded JSON record and exits.

Actions (all under a stable sibling ``flock``, taken non-blocking so a busy
state refuses immediately instead of delaying the login):

    arm      write an ``armed`` record before the compositor starts
    consume  atomically armed -> pending for one systemd invocation, immediately
             before ``exec`` (call in-process, or through the CLI)
    ready    pending -> ready from ExecStartPost once the real Shell is up; the
             capture opt-in is read from ``/proc/MAINPID/environ`` and the four
             private Mutter libraries must be mapped from ROOT/lib, so the
             helper's own environment can never stand in for the compositor's
    finish   ready -> armed only after a clean stop; every other exit blocks
    disarm   write a blocking marker so late systemd hooks cannot re-arm
    status   read-only report; never writes or executes

Command line, matching the systemd unit hook points:

    /usr/bin/python3 -I <root>/startup_guard.py ACTION \
        --runtime ROOT --mode MODE --state ABS_STATE_FILE

Success prints one bounded JSON record on stdout. A refused operation prints a
single fixed ``SocketStream guard: <reason>`` line on stderr and exits nonzero.
``ROOT`` must already have passed the runtime validator and startup probe; this
module never re-validates the runtime payload.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

SCHEMA = 1
STATE_SUFFIX = ".json"
LOCK_SUFFIX = ".lock"
MAX_RECORD = 8192
MAX_ENVIRON = 1024 * 1024
MAX_MAPS = 8 * 1024 * 1024
PHASES = ("armed", "pending", "ready", "blocked")
REASONS = ("clean-stop", "unclean-stop", "pending-failed", "disarmed")
IDENTITY_KEYS = ("invocation_id", "boot_id", "pid", "starttime")
ENVIRONMENT_KEYS = ("SOCKET_STREAM_CAPTURE_EXCLUDE", "INVOCATION_ID")
MAPPING_LIBRARIES = tuple(f"lib/{name}.so.0" for name in (
    "libmutter-18", "libmutter-clutter-18", "libmutter-cogl-18", "libmutter-mtk-18",
))

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,159}\Z")
MODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")
INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
BOOT_ID = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
ABSOLUTE = re.compile(r"/[^\x00-\x1f]{0,4095}\Z")
PROCESS_ID = re.compile(r"[1-9][0-9]{0,9}\Z")

# Fixed diagnostics. Never interpolate untrusted text into them: they end up in
# the session journal and must stay actionable without leaking state.
INVALID_ROOT = "runtime root must be a canonical absolute directory in a trusted tree"
IDENTITY_MISMATCH = "state record belongs to a different runtime or mode"
STATE_INVALID = "state record is invalid"
STATE_UNSAFE = "state record has unsafe ownership, permissions, or type"
DIRECTORY_UNSAFE = "state directory has unsafe ownership or permissions"
DIRECTORY_MISSING = "state directory is unavailable"
LOCK_UNSAFE = "state lock has unsafe ownership, permissions, or type"
NOT_ARMED = "no armed startup record; run activation again"
ALREADY_DECIDED = "startup decision was already consumed"
BLOCKED = "startup record is blocked; run a new passing probe before arming"
STILL_LIVE = "a custom compositor invocation is still live"
INVOCATION_INVALID = "INVOCATION_ID must be 32 lowercase hex characters"
INVOCATION_MISMATCH = "INVOCATION_ID does not match the recorded startup"
NOT_PENDING = "ready requires a matching pending startup"
MAIN_PID_MISMATCH = "MAINPID does not match the recorded main process"
NOT_CUSTOM_SHELL = "main process is not the custom runtime launcher"
EXCLUSION_MISSING = "capture exclusion opt-in is missing from the main service"
BOOT_CHANGED = "boot identifier changed; the startup record is stale"
PROCESS_UNKNOWN = "main process identity could not be verified"
PROCESS_FOREIGN = "main process belongs to another user"
PROCESS_INVALID = "process identifier is invalid"
ENVIRONMENT_INVALID = "main process environment is unavailable or not verifiable"
MAPPINGS_INVALID = "main process did not map the private Mutter family"
WRITE_FAILED = "state record could not be written durably"
BUSY = "startup state is busy; another guard action is running"
PHASE_REFUSED = "startup record cannot perform this action"
NO_RECORD = "no startup record for this invocation"
USAGE_INVALID = "unsupported startup guard invocation"
INCOMPLETE = "startup guard could not complete"


class GuardError(Exception):
    """A fixed, actionable diagnostic safe to show in the session journal."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Proc:
    """Production ``/proc`` view. Tests substitute their own fixture root."""

    root: Path = Path("/proc")
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")

    def boot(self) -> str:
        try:
            text = self.boot_id_path.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError) as exc:
            raise GuardError(BOOT_CHANGED) from exc
        if not BOOT_ID.fullmatch(text):
            raise GuardError(BOOT_CHANGED)
        return text

    def starttime(self, pid: int):
        """Field 22 of /proc/PID/stat, or None when the process is gone."""
        try:
            text = (self.root / str(pid) / "stat").read_text(
                encoding="ascii", errors="replace")
        except OSError:
            return None
        head, separator, tail = text.rpartition(")")
        if not separator or not head.strip():
            return None
        fields = tail.split()
        # tail[0] is field 3 (state), so the process start time is tail[19].
        if len(fields) < 20:
            return None
        try:
            value = int(fields[19])
        except ValueError:
            return None
        return value if value > 0 else None

    def executable(self, pid: int):
        """Device/inode of /proc/PID/exe, or None when it cannot be resolved."""
        try:
            info = os.stat(self.root / str(pid) / "exe")
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_dev, info.st_ino)

    def owner(self, pid: int):
        """UID that owns /proc/PID, or None when it cannot be established."""
        try:
            info = os.stat(self.root / str(pid))
        except OSError:
            return None
        return info.st_uid if stat.S_ISDIR(info.st_mode) else None

    def environment(self, pid: int, names=ENVIRONMENT_KEYS) -> dict:
        """Only the named entries of /proc/PID/environ; nothing else escapes.

        The caller passes a fixed whitelist so unrelated secrets are never
        parsed, returned, or logged. An unreadable or oversized block raises
        instead of being guessed at.
        """
        prefixes = {name: name.encode("ascii") + b"=" for name in names}
        try:
            descriptor = os.open(
                self.root / str(pid) / "environ",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise GuardError(ENVIRONMENT_INVALID) from exc
        chunks = []
        total = 0
        try:
            while total <= MAX_ENVIRON:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except OSError as exc:
            raise GuardError(ENVIRONMENT_INVALID) from exc
        finally:
            os.close(descriptor)
        if total > MAX_ENVIRON:
            raise GuardError(ENVIRONMENT_INVALID)
        values = {}
        for entry in b"".join(chunks).split(b"\0"):
            for name, prefix in prefixes.items():
                if entry.startswith(prefix):
                    values[name] = entry[len(prefix):].decode("ascii", "replace")
        return values

    def mappings(self, pid: int) -> bytes:
        """Bounded ``/proc/PID/maps`` bytes; never returns more than MAX_MAPS.

        An unreadable or oversized block raises a fixed refusal instead of being
        partially trusted. The raw text never leaves the caller.
        """
        try:
            descriptor = os.open(
                self.root / str(pid) / "maps",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise GuardError(MAPPINGS_INVALID) from exc
        chunks = []
        total = 0
        try:
            while total <= MAX_MAPS:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except OSError as exc:
            raise GuardError(MAPPINGS_INVALID) from exc
        finally:
            os.close(descriptor)
        if total > MAX_MAPS:
            raise GuardError(MAPPINGS_INVALID)
        return b"".join(chunks)


PROC = Proc()


# --- path and record validation -------------------------------------------


def _trusted_directory(info, uid: int, *, ancestor: bool) -> bool:
    """Directory we can still trust after the caller's own ownership check.

    A root-owned sticky ancestor (``/tmp``) cannot replace our directory, so it
    is the only accepted group/other-writable case.
    """
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
        return False
    if info.st_mode & 0o022:
        return ancestor and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    return True


def _walk_trusted(path: Path, uid: int, code: str) -> None:
    current = path
    ancestor = False
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise GuardError(code) from exc
        if not _trusted_directory(info, uid, ancestor=ancestor):
            raise GuardError(code)
        if current.parent == current:
            return
        current = current.parent
        ancestor = True


def _runtime_root(runtime, uid: int) -> Path:
    """ROOT is already validated; here only its pinned identity is checked."""
    if not isinstance(runtime, (str, os.PathLike)):
        raise GuardError(INVALID_ROOT)
    path = Path(runtime)
    if not path.is_absolute():
        raise GuardError(INVALID_ROOT)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GuardError(INVALID_ROOT) from exc
    if resolved != path or not IDENTIFIER.fullmatch(resolved.name):
        raise GuardError(INVALID_ROOT)
    _walk_trusted(resolved, uid, INVALID_ROOT)
    return resolved


def _mode(mode) -> str:
    if not isinstance(mode, str) or not MODE.fullmatch(mode):
        raise GuardError(USAGE_INVALID)
    return mode


def _state_path(state) -> Path:
    path = Path(state)
    if (not path.is_absolute() or not path.name.endswith(STATE_SUFFIX)
            or len(path.name) > 96 or path.parent == path
            or "." in path.parts or ".." in path.parts):
        raise GuardError(USAGE_INVALID)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise GuardError(STATE_UNSAFE) from exc
    if not stat.S_ISREG(info.st_mode):
        raise GuardError(STATE_UNSAFE)
    return path


def lock_name(state: Path) -> str:
    """Stable sibling lock; created once and never unlinked."""
    return state.name + LOCK_SUFFIX


def _descend(descriptor: int, component: str, uid: int, *, create: bool, last: bool):
    """Open one verified component relative to its already verified parent.

    A missing component is created at 0700. A collision (including one raced in
    by another process) is opened with ``O_NOFOLLOW`` and validated; it is
    never chmodded, so an unrelated directory keeps its mode and contents. A
    component we definitely created is pinned back to 0700 with ``fchmod`` on
    the freshly opened descriptor, so a restrictive umask cannot leave a
    private directory unreadable to its owner.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    created_identity = None
    try:
        child = os.open(component, flags, dir_fd=descriptor)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(component, 0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise GuardError(DIRECTORY_MISSING) from exc
        else:
            try:
                made = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                created_identity = (made.st_dev, made.st_ino)
            except OSError:
                created_identity = None
        try:
            child = os.open(component, flags, dir_fd=descriptor)
        except OSError as exc:
            raise GuardError(DIRECTORY_UNSAFE) from exc
    except OSError as exc:
        raise GuardError(DIRECTORY_UNSAFE) from exc
    info = os.fstat(child)
    if (created_identity is not None
            and stat.S_ISDIR(info.st_mode) and info.st_uid == uid
            and (info.st_dev, info.st_ino) == created_identity):
        try:
            os.fchmod(child, 0o700)
        except OSError as exc:
            os.close(child)
            raise GuardError(DIRECTORY_UNSAFE) from exc
        info = os.fstat(child)
    if (not _trusted_directory(info, uid, ancestor=not last)
            or (last and stat.S_IMODE(info.st_mode) != 0o700)):
        os.close(child)
        raise GuardError(DIRECTORY_UNSAFE)
    return child


def _open_state_directory(state: Path, uid: int, *, create: bool):
    """Walk the dedicated state directory one descriptor at a time.

    Every step is relative to its parent's descriptor with ``O_NOFOLLOW`` and
    ``O_DIRECTORY``, so a symlink or a swap anywhere in the path is refused
    instead of followed. Using the resulting descriptor for the record, the
    lock, and the atomic replace keeps those writes inside the directory that
    was actually validated.
    """
    components = state.parent.parts
    if len(components) < 2 or components[0] != "/":
        raise GuardError(DIRECTORY_UNSAFE)
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise GuardError(DIRECTORY_UNSAFE) from exc
    try:
        for index in range(1, len(components)):
            child = _descend(descriptor, components[index], uid, create=create,
                             last=index == len(components) - 1)
            os.close(descriptor)
            descriptor = child
            if child is None:
                return None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_lock(directory: int, state: Path, uid: int) -> int:
    name = lock_name(state)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    try:
        descriptor = os.open(
            name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        created = True
    except FileExistsError:
        # A collision is reopened without O_CREAT and never chmodded.
        try:
            descriptor = os.open(name, flags, dir_fd=directory)
        except OSError as exc:
            raise GuardError(LOCK_UNSAFE) from exc
    except OSError as exc:
        raise GuardError(LOCK_UNSAFE) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            raise GuardError(LOCK_UNSAFE)
        if created:
            # O_EXCL guarantees this descriptor is the file we just created, so
            # pinning 0600 here cannot touch an unrelated entry.
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as exc:
                raise GuardError(LOCK_UNSAFE) from exc
            info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GuardError(LOCK_UNSAFE)
        try:
            # Never delay a login: a held lock refuses immediately and the
            # caller starts stock GNOME instead.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GuardError(BUSY) from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _locked(state: Path, uid: int, *, create: bool):
    directory = _open_state_directory(state, uid, create=create)
    if directory is None:
        raise GuardError(DIRECTORY_MISSING)
    lock = None
    try:
        lock = _acquire_lock(directory, state, uid)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _validate(record) -> dict:
    if not isinstance(record, dict):
        raise GuardError(STATE_INVALID)
    allowed = {"schema", "runtime", "mode", "phase", "reason", *IDENTITY_KEYS}
    if set(record) - allowed:
        raise GuardError(STATE_INVALID)
    if type(record.get("schema")) is not int or record["schema"] != SCHEMA:
        raise GuardError(STATE_INVALID)
    runtime = record.get("runtime")
    if (not isinstance(runtime, str) or not ABSOLUTE.fullmatch(runtime)
            or not IDENTIFIER.fullmatch(Path(runtime).name)):
        raise GuardError(STATE_INVALID)
    mode = record.get("mode")
    if not isinstance(mode, str) or not MODE.fullmatch(mode):
        raise GuardError(STATE_INVALID)
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        raise GuardError(STATE_INVALID)
    if "reason" in record and record["reason"] not in REASONS:
        raise GuardError(STATE_INVALID)
    present = [key for key in IDENTITY_KEYS if key in record]
    if present and len(present) != len(IDENTITY_KEYS):
        raise GuardError(STATE_INVALID)
    if phase in ("pending", "ready") and not present:
        raise GuardError(STATE_INVALID)
    if phase == "armed" and present:
        raise GuardError(STATE_INVALID)
    if present:
        if (not isinstance(record["invocation_id"], str)
                or not INVOCATION.fullmatch(record["invocation_id"])
                or not isinstance(record["boot_id"], str)
                or not BOOT_ID.fullmatch(record["boot_id"])
                or type(record["pid"]) is not int or record["pid"] < 1
                or type(record["starttime"]) is not int or record["starttime"] < 1):
            raise GuardError(STATE_INVALID)
    return record


def _read(directory: int, state: Path, uid: int):
    try:
        descriptor = os.open(
            state.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GuardError(STATE_UNSAFE) from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise GuardError(STATE_UNSAFE)
        if info.st_size < 1 or info.st_size > MAX_RECORD:
            raise GuardError(STATE_INVALID)
        chunks = []
        remaining = info.st_size
        try:
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise GuardError(STATE_UNSAFE) from exc
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) != info.st_size:
        raise GuardError(STATE_INVALID)
    try:
        record = json.loads(data.decode("ascii"), object_pairs_hook=_unique_fields)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise GuardError(STATE_INVALID) from None
    return _validate(record)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GuardError(STATE_INVALID)
        result[key] = value
    return result


def _encode(record: dict) -> bytes:
    text = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    data = (text + "\n").encode("ascii")
    if len(data) > MAX_RECORD:
        raise GuardError(STATE_INVALID)
    return data


def _sync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _sync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _write_atomic(directory: int, state: Path, data: bytes) -> None:
    temporary = f".{state.name}.{os.urandom(8).hex()}.tmp"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=directory)
    except OSError as exc:
        raise GuardError(WRITE_FAILED) from exc
    try:
        # O_EXCL means this descriptor is our own new file: pin 0600 so a
        # restrictive umask cannot leave the committed record unreadable.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            _sync_file(stream.fileno())
        os.replace(temporary, state.name, src_dir_fd=directory, dst_dir_fd=directory)
        _sync_directory(directory)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary, dir_fd=directory)
        except OSError:
            pass
        raise GuardError(WRITE_FAILED) from exc


def _store(directory: int, state: Path, record: dict, previous) -> None:
    """Durable replace; restore the previous record when durability fails."""
    data = _encode(record)
    try:
        _write_atomic(directory, state, data)
    except GuardError:
        if previous is not None:
            try:
                _write_atomic(directory, state, _encode(previous))
            except GuardError:
                pass
        raise


def _current(directory: int, state: Path, uid: int, root: Path, mode: str):
    record = _read(directory, state, uid)
    if record is not None and (record["runtime"], record["mode"]) != (str(root), mode):
        raise GuardError(IDENTITY_MISMATCH)
    return record


# --- process and invocation identity --------------------------------------


def _environment(environ):
    return os.environ if environ is None else environ


def _process_id(value) -> int:
    if type(value) is not int or not PROCESS_ID.fullmatch(str(value)):
        raise GuardError(PROCESS_INVALID)
    return value


def _invocation(environment) -> str:
    value = environment.get("INVOCATION_ID")
    if not isinstance(value, str) or not INVOCATION.fullmatch(value):
        raise GuardError(INVOCATION_INVALID)
    return value


def _main_pid(environment) -> int:
    value = environment.get("MAINPID")
    if not isinstance(value, str) or not PROCESS_ID.fullmatch(value):
        raise GuardError(PROCESS_INVALID)
    return int(value)


def _file_identity(path: Path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None


def _mapped_libraries(root: Path, pid: int, proc: Proc) -> list:
    """Every segment of every Mutter family mapping must be our exact inode.

    Concise duplicate of the startup probe's check so the guard never imports a
    sibling probe at runtime. Any foreign family name, missing private library,
    deleted mapping, or device/inode/path mismatch is a fixed refusal; the raw
    maps text is never returned or logged.
    """
    expected = {}
    for name in MAPPING_LIBRARIES:
        file = root / name
        try:
            info = os.stat(file)
        except OSError as exc:
            raise GuardError(MAPPINGS_INVALID) from exc
        expected[file.name] = (
            file, (os.major(info.st_dev), os.minor(info.st_dev)), info.st_ino)
    data = proc.mappings(pid)
    found = set()
    for line in data.decode("utf-8", "replace").splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        pathname = fields[5]
        basename = Path(pathname.removesuffix(" (deleted)")).name
        if not basename.startswith("libmutter"):
            continue
        item = expected.get(basename)
        if item is None or pathname.endswith(" (deleted)"):
            raise GuardError(MAPPINGS_INVALID)
        file, device, inode = item
        try:
            actual_device = tuple(int(part, 16) for part in fields[3].split(":"))
            matches = (Path(pathname) == file and actual_device == device
                       and int(fields[4]) == inode)
        except ValueError:
            matches = False
        if not matches:
            raise GuardError(MAPPINGS_INVALID)
        found.add(basename)
    if found != set(expected):
        raise GuardError(MAPPINGS_INVALID)
    return sorted(str(expected[name][0]) for name in found)


def _alive(record: dict, root: Path, proc: Proc) -> bool:
    """Boot plus exact start time identifies one process; never a reused PID."""
    try:
        if proc.boot() != record["boot_id"]:
            return False
    except GuardError:
        return False
    if proc.starttime(record["pid"]) != record["starttime"]:
        return False
    if record["phase"] == "ready":
        return proc.executable(record["pid"]) == _file_identity(root / "gnome-shell")
    return True


def _base(root: Path, mode: str, phase: str, **extra) -> dict:
    record = {"schema": SCHEMA, "runtime": str(root), "mode": mode, "phase": phase}
    record.update(extra)
    return record


# --- public actions -------------------------------------------------------


def arm(state: Path, runtime: Path, mode: str, *, proc: Proc = PROC) -> dict:
    """Write an ``armed`` record; refuse while a custom invocation is live."""
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    with _locked(path, uid, create=True) as directory:
        current = _current(directory, path, uid, root, mode)
        if (current is not None and current["phase"] in ("pending", "ready")
                and _alive(current, root, proc)):
            raise GuardError(STILL_LIVE)
        record = _base(root, mode, "armed")
        _store(directory, path, record, current)
        return record


def consume(state: Path, runtime: Path, mode: str, *, environ=None, pid=None,
            proc: Proc = PROC) -> dict:
    """Atomically claim the armed record for one systemd invocation.

    Called in the main process immediately before ``exec``; the claim is
    fsynced before returning so a power loss cannot replay the decision.
    """
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    environment = _environment(environ)
    invocation = _invocation(environment)
    target = _process_id(os.getpid() if pid is None else pid)
    with _locked(path, uid, create=False) as directory:
        current = _current(directory, path, uid, root, mode)
        if current is None:
            raise GuardError(NOT_ARMED)
        if current["phase"] == "blocked":
            raise GuardError(BLOCKED)
        if current["phase"] != "armed":
            raise GuardError(ALREADY_DECIDED)
        starttime = proc.starttime(target)
        if starttime is None:
            raise GuardError(PROCESS_UNKNOWN)
        record = _base(
            root, mode, "pending", invocation_id=invocation, boot_id=proc.boot(),
            pid=target, starttime=starttime)
        _store(directory, path, record, current)
        return record


def ready(state: Path, runtime: Path, mode: str, *, environ=None,
          proc: Proc = PROC) -> dict:
    """Promote pending -> ready only for the recorded main process.

    The opt-in is read from ``/proc/MAINPID/environ``: the launcher exports it
    only into the compositor it execs, so the systemd helper's own environment
    never carries it and can never stand in for the compositor's.
    """
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    environment = _environment(environ)
    invocation = _invocation(environment)
    main_pid = _main_pid(environment)
    with _locked(path, uid, create=False) as directory:
        current = _current(directory, path, uid, root, mode)
        if current is None or current["phase"] != "pending":
            # A stock fallback also reaches ExecStartPost; it must never promote.
            raise GuardError(NOT_PENDING)
        if current["invocation_id"] != invocation:
            raise GuardError(INVOCATION_MISMATCH)
        if current["pid"] != main_pid:
            raise GuardError(MAIN_PID_MISMATCH)
        if proc.boot() != current["boot_id"]:
            raise GuardError(BOOT_CHANGED)
        owner = proc.owner(main_pid)
        if owner is None:
            raise GuardError(PROCESS_UNKNOWN)
        if owner != uid:
            raise GuardError(PROCESS_FOREIGN)
        if proc.starttime(main_pid) != current["starttime"]:
            raise GuardError(PROCESS_UNKNOWN)
        if proc.executable(main_pid) != _file_identity(root / "gnome-shell"):
            raise GuardError(NOT_CUSTOM_SHELL)
        exported = proc.environment(main_pid)
        if exported.get("SOCKET_STREAM_CAPTURE_EXCLUDE") != "1":
            raise GuardError(EXCLUSION_MISSING)
        if exported.get("INVOCATION_ID", invocation) != invocation:
            raise GuardError(INVOCATION_MISMATCH)
        _mapped_libraries(root, main_pid, proc)
        record = _base(
            root, mode, "ready", invocation_id=invocation, boot_id=current["boot_id"],
            pid=main_pid, starttime=current["starttime"])
        _store(directory, path, record, current)
        return record


def finish(state: Path, runtime: Path, mode: str, *, environ=None,
           proc: Proc = PROC) -> dict:
    """ExecStopPost: re-arm only after a clean main-process stop."""
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    environment = _environment(environ)
    invocation = _invocation(environment)
    with _locked(path, uid, create=False) as directory:
        current = _current(directory, path, uid, root, mode)
        if current is None:
            raise GuardError(NO_RECORD)
        if current.get("invocation_id") != invocation:
            # A late hook from an older run must never touch a later record.
            raise GuardError(INVOCATION_MISMATCH)
        if current["phase"] == "pending":
            record = _base(
                root, mode, "blocked", reason="pending-failed",
                invocation_id=current["invocation_id"], boot_id=current["boot_id"],
                pid=current["pid"], starttime=current["starttime"])
            _store(directory, path, record, current)
            return record
        if current["phase"] != "ready":
            raise GuardError(PHASE_REFUSED)
        clean = (environment.get("SERVICE_RESULT") == "success"
                 and environment.get("EXIT_CODE") == "exited"
                 and environment.get("EXIT_STATUS") == "0")
        if clean:
            record = _base(root, mode, "armed", reason="clean-stop")
        else:
            # SuccessExitStatus=1, signals, timeouts, OOM kills and a missing
            # SERVICE_RESULT all leave a blocked marker behind.
            record = _base(
                root, mode, "blocked", reason="unclean-stop",
                invocation_id=current["invocation_id"], boot_id=current["boot_id"],
                pid=current["pid"], starttime=current["starttime"])
        _store(directory, path, record, current)
        return record


def disarm(state: Path, runtime: Path, mode: str, *, proc: Proc = PROC) -> dict:
    """Removal path: leave a blocking marker that late hooks cannot undo."""
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    with _locked(path, uid, create=True) as directory:
        previous = _read(directory, path, uid)
        record = _base(root, mode, "blocked", reason="disarmed")
        _store(directory, path, record, previous)
        return record


def status(state: Path, runtime: Path, mode: str, *, proc: Proc = PROC) -> dict:
    """Read-only report. Never creates, locks, or rewrites anything."""
    uid = os.getuid()
    root = _runtime_root(runtime, uid)
    mode = _mode(mode)
    path = _state_path(state)
    directory = _open_state_directory(path, uid, create=False)
    if directory is None:
        return _base(root, mode, "missing")
    try:
        record = _read(directory, path, uid)
    finally:
        os.close(directory)
    if record is None:
        return _base(root, mode, "missing")
    report = dict(record)
    same = (record["runtime"], record["mode"]) == (str(root), mode)
    report["identity"] = "same" if same else "different"
    return report


ACTIONS = {
    "arm": arm, "consume": consume, "ready": ready,
    "finish": finish, "disarm": disarm, "status": status,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot startup guard for the patched GNOME Shell runtime.")
    parser.add_argument("action", choices=tuple(ACTIONS))
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--pid", type=int, help="override the recording PID (testing)")
    args = parser.parse_args(argv)
    try:
        if args.action == "consume":
            record = consume(Path(args.state), Path(args.runtime), args.mode, pid=args.pid)
        else:
            record = ACTIONS[args.action](Path(args.state), Path(args.runtime), args.mode)
    except GuardError as exc:
        print(f"SocketStream guard: {exc.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print(f"SocketStream guard: {INCOMPLETE}", file=sys.stderr)
        return 1
    print(json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    if args.action == "finish" and record["phase"] != "armed":
        print("SocketStream guard: startup record was not re-armed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
