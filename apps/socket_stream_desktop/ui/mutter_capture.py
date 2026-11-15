"""Custom Mutter capture-filter runtime: detection and managed activation.

Pure standard library; no GTK, no D-Bus binding, no Wayland client.  The GTK
shell (:mod:`capture_rules`) uses this module to answer three different
questions without ever blending them:

``deployed``
    The custom runtime manifest exists, parses, and every file it lists still
    matches its recorded size and SHA-256.  This is a statement about the
    filesystem, not about the compositor.

``configured``
    A managed systemd ``--user`` drop-in is installed for the *exact* GNOME
    Shell unit that runs this session, it is byte-identical to the override
    rendered for *this* runtime version directory, and the unit's effective
    ``ExecStart`` is that same override.

``runtime_active``
    Live evidence from the running compositor: the owning GNOME Shell process
    has ``SOCKET_STREAM_CAPTURE_EXCLUDE=1`` in **its own** ``/proc`` environ
    and has *all four* required Mutter libraries mapped from the validated
    version directory, with matching device/inode, no ``(deleted)`` mapping,
    and no Mutter library mapped from anywhere else.  A shell that still runs
    the system Mutter libraries is reported as ``stock``, not as a failure.

Pixel-level exclusion is deliberately *not* claimed: only a real whole-screen
share can show that.  ``verified`` therefore stays false everywhere.

Runtime contract (fixed, shared with the native staging tools)
--------------------------------------------------------------
The app mirrors ``native/mutter-capture-filter/validate_runtime.py`` so the two
can never disagree about whether a runtime is deployable:

* Default manifest ``/opt/socket_stream-mutter/current/runtime.json``; the path
  is resolved through symlinks to the immutable version directory.
  ``SOCKET_STREAM_MUTTER_RUNTIME`` may point at an absolute manifest path.
* ``schema`` is the integer ``2``; a schema-1 runtime is reported as superseded
  and must be rebuilt.  ``runtime_id``/``source_version``/``source_sha256``/
  ``patch_sha256`` are validated by the same regular expressions, and
  ``runtime_id`` must equal the canonical content-bound identifier the stager
  computes.
* ``shell`` is exactly ``"gnome-shell"`` and the runtime ships its own copy of
  that executable with a corrected RUNPATH, alongside ``launch-gnome-shell``;
  both must carry the owner execute bit.
* ``packages`` pins exactly the six shell/Mutter packages, every non-shell pin
  equals ``source_version``, and each installed package must report the dpkg
  status ``install ok installed`` at exactly that version.
* ``library_dir``/``typelib_dir`` are fixed, ``libraries`` is exactly the four
  SONAMEs, and both loader directories contain exactly their four entries.
* ``files`` is exactly the four libraries, the four typelibs,
  ``validate_runtime.py``, ``launch-gnome-shell``, ``startup_probe.py``,
  ``startup_guard.py`` and ``gnome-shell``, plus optionally
  ``source.dsc``/``capture.patch`` whose hashes must match the provenance
  fields.
* Every payload is a regular file (``lstat``, so a symlink is refused), and
  the runtime directory and *all* its ancestors are trusted: owned by root in
  production and never writable by anybody else.
* Nothing is executed to compute status.  The runtime's own launcher remains
  the enforcement point at login; the app only reads ``/proc`` and the guard's
  small JSON record.

Activation design (immutable, one version directory per activation)
------------------------------------------------------------------
* The drop-in names the *resolved* version directory
  (``.../50.1-0ubuntu2.5.socket-stream.<id>``), never the mutable
  ``.../current`` pointer.  A republished ``current`` therefore requires an
  explicit "Set up exclusion" again, and the override never changes meaning
  behind the user's back.
* The schema-2 override pins the absolute guard state path and adds an
  ``ExecStartPost`` ready hook and an ``ExecStopPost`` finish hook.  It never
  resets the upstream unit's own ``ExecStopPost``, ``Type``, ``NotifyAccess``,
  ``Restart`` or ``OnFailure``.
* ``configured`` needs the exact rendered file for this unit, an effective
  ``ExecStart`` naming the same directory, state and mode, and both lifecycle
  hooks present and unambiguous in the effective unit.
* A schema-1 override stays recognisable, removable and migratable, but never
  counts as configured.
* A stale managed override stays removable even when the runtime or its
  packages are gone, and GNOME on X11 still offers that removal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

Runner = Callable[..., object]
Which = Callable[[str], Optional[str]]

DEFAULT_MANIFEST = "/opt/socket_stream-mutter/current/runtime.json"
RUNTIME_ENVIRON = "SOCKET_STREAM_MUTTER_RUNTIME"
OPT_IN_ENVIRON = "SOCKET_STREAM_CAPTURE_EXCLUDE"

MANAGED_FILENAME = "90-socket_stream-capture.conf"
STOCK_SHELL = "/usr/bin/gnome-shell"
MANAGED_HEADER = "# Managed by SocketStream"

UNIT_PATTERN = re.compile(r"^org\.gnome\.Shell@([A-Za-z0-9_.\-]+)\.service$")
MODE_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")
# Characters that systemd unit files and ld.so tolerate without escaping.
SAFE_PATH_PATTERN = re.compile(r"/[A-Za-z0-9/._+-]+\Z")

SCHEMA = 2
# The four SONAME libraries and the four GObject-introspection typelibs the
# runtime stages, exactly as the native validator names them.
LIBRARIES = tuple(f"lib/{name}.so.0" for name in (
    "libmutter-18", "libmutter-clutter-18", "libmutter-cogl-18", "libmutter-mtk-18",
))
TYPELIBS = tuple(f"typelib/{name}-18.typelib" for name in ("Meta", "Clutter", "Cogl", "Mtk"))
# Schema 2 adds the private startup probe and guard helpers, and ships its own
# copy of the GNOME Shell executable with a corrected RUNPATH.
SCRIPTS = (
    "validate_runtime.py",
    "launch-gnome-shell",
    "startup_probe.py",
    "startup_guard.py",
)
SHELL = "gnome-shell"
EXECUTABLES = (SHELL, "launch-gnome-shell")
PROVENANCE = ("source.dsc", "capture.patch")
# The same six packages the native validator pins: the shell, the Mutter
# libraries, the Mutter runtime data/typelibs, and their shared data packages.
REQUIRED_PACKAGES = (
    "gnome-shell",
    "gnome-shell-common",
    "libmutter-18-0",
    "mutter-common",
    "mutter-common-bin",
    "gir1.2-mutter-18",
)

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,159}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_\-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

MUTTER_LIBRARY_PREFIX = "libmutter"

MAX_MANIFEST_BYTES = 65536
MAX_RUNTIME_FILE_BYTES = 256 * 1024 * 1024
MAX_ENVIRON_BYTES = 1 << 20
MAX_MAPS_BYTES = 8 << 20
MAX_PROC_ENTRIES = 4096
MAX_DROPIN_DIRS = 32
# A managed override is four comment lines plus two service lines; anything
# far larger at that name is not ours and is left alone.
MAX_DROPIN_BYTES = 16384

# The startup guard's state record and the private probe it gates.  The guard
# is a helper inside the runtime directory; it is only ever executed by the
# systemd unit, never by status.  The app reads its JSON record directly.
GUARD_ACTIONS = ("arm", "disarm", "status", "ready", "finish")
GUARD_PHASES = ("armed", "pending", "ready", "blocked")
GUARD_MISSING = "missing"
GUARD_STATE_PARTS = ("socket_stream", "mutter-startup")
MAX_GUARD_BYTES = 8192
GUARD_IDENTITY = ("invocation_id", "boot_id", "pid", "starttime")
GUARD_REASONS = ("clean-stop", "unclean-stop", "pending-failed", "disarmed")
INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
BOOT_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
# The private startup probe allows 40s of startup plus a bounded cleanup; the
# app runs it before every install or rearm and waits for the whole budget.
PROBE_TIMEOUT = 80
GUARD_TIMEOUT = 30

_ENVIRON_KEYS = (OPT_IN_ENVIRON, "XDG_SESSION_ID", "WAYLAND_DISPLAY")

_HASH_CACHE: dict = {}
_ANY_CONTENT = object()


class MutterCaptureError(RuntimeError):
    """Activation could not be read, written, or removed."""


class _Invalid(Exception):
    """One fixed, non-content-bearing validation failure."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _run(argv, timeout=15, env=None):
    import subprocess

    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, env=env,
        check=False,
    )


def _text(result) -> str:
    if result is None:
        return ""
    return ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")).strip()


def _returncode(result) -> int:
    try:
        return int(getattr(result, "returncode", 1))
    except (TypeError, ValueError):
        return 1


def _error_name(exc: BaseException) -> str:
    # Fixed, non-content-bearing description for status text.
    return getattr(exc, "strerror", None) or type(exc).__name__


def _dev_text(st_dev: int) -> str:
    """/proc/<pid>/maps renders a device as ``major:minor`` in hex."""
    return f"{os.major(st_dev):02x}:{os.minor(st_dev):02x}"


def _stamp(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _user_directory(path: Path, *, create=False, private=False):
    """Pin a trusted user directory without following any symlink component."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise MutterCaptureError("the configuration path must be a canonical absolute directory")
    uid = os.getuid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for index, component in enumerate(path.parts[1:], 1):
            created_identity = None
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    made = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                    created_identity = (made.st_dev, made.st_ino)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(child)
            if (created_identity == (info.st_dev, info.st_ino)
                    and stat.S_ISDIR(info.st_mode) and info.st_uid == uid):
                os.fchmod(child, 0o700)
                info = os.fstat(child)
            final = index == len(path.parts) - 1
            sticky = not final and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
                    or (info.st_mode & 0o022 and not sticky)
                    or (final and info.st_uid != uid)
                    or (final and private and stat.S_IMODE(info.st_mode) != 0o700)):
                raise MutterCaptureError("the configuration directory has unsafe ownership or permissions")
        yield descriptor
    finally:
        os.close(descriptor)


def _read_user_file(directory: int, name: str, limit: int, *, private=False):
    """Read one owned, bounded regular file through its pinned directory."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022
                or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise MutterCaptureError("the configuration file has unsafe ownership, permissions, or type")
        if info.st_size > limit:
            raise MutterCaptureError("the configuration file exceeds its size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if (len(data) > limit or _stamp(os.fstat(descriptor)) != _stamp(info)
                or _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _stamp(info)):
            raise MutterCaptureError("the configuration file changed while it was read")
        return data
    finally:
        os.close(descriptor)


def _replace_user_file(directory: int, name: str, data: bytes, *, expected=_ANY_CONTENT,
                       limit=MAX_DROPIN_BYTES, private=False):
    """Durable replace inside the validated directory; never chmod a collision."""
    if expected is not _ANY_CONTENT and _read_user_file(directory, name, limit, private=private) != expected:
        raise MutterCaptureError("the configuration file changed during the operation; it was left untouched")
    temporary = "." + name + "." + os.urandom(8).hex() + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not _ANY_CONTENT and _read_user_file(directory, name, limit, private=private) != expected:
            raise MutterCaptureError("the configuration file changed during the operation; it was left untouched")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _trusted_directory(path: Path, uid: int) -> bool:
    """The directory and every ancestor must be root-owned (production).

    Mirrors ``trusted_directory`` in the native validator, including the
    exception for a root-owned sticky ancestor such as ``/tmp``: a sticky
    directory cannot be renamed away by another user, only used to create an
    entry whose name the attacker does not control here.
    """
    try:
        chain = (path, *path.parents)
    except (OSError, ValueError):
        return False
    for current in chain:
        try:
            info = current.lstat()
        except OSError:
            return False
        sticky_parent = (
            current != path
            and info.st_uid == 0
            and bool(info.st_mode & stat.S_ISVTX)
        )
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
                or (info.st_mode & 0o022 and not sticky_parent)):
            return False
    return True


def _regular(path: Path, uid: int):
    """``lstat`` a payload: regular file, owned by ``uid``, not group/other writable."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or info.st_mode & 0o022):
        return None
    return info


def _file_digest(path: Path, uid: int) -> tuple:
    """Return ``(digest, size, problem)``; exactly one of digest/problem is None.

    The cache key includes ``st_ctime_ns`` as well as mtime, and the file is
    re-stamped before opening, through ``fstat`` while open, and by ``lstat``
    afterwards.  Rewriting a file with the same size and an artificial mtime
    therefore cannot serve a stale digest.
    """
    before = _regular(path, uid)
    if before is None:
        return None, 0, "has unsafe ownership, permissions, or type"
    if before.st_size < 1 or before.st_size > MAX_RUNTIME_FILE_BYTES:
        return None, 0, "has an unsupported size"
    key = (str(path),) + _stamp(before)
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached, before.st_size, None

    digest = sha256()
    try:
        with open(path, "rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                return None, 0, "changed while it was being read"
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                return None, 0, "changed while it was being read"
    except OSError:
        return None, 0, "could not be read"
    try:
        if _stamp(path.lstat()) != _stamp(before):
            return None, 0, "changed while it was being read"
    except OSError:
        return None, 0, "disappeared while it was being read"

    value = digest.hexdigest()
    if len(_HASH_CACHE) > 64:
        _HASH_CACHE.clear()
    _HASH_CACHE[key] = value
    return value, before.st_size, None


def _read_environ(proc_root: Path, pid: int,
                  keys: Iterable[str] = _ENVIRON_KEYS) -> Optional[dict]:
    """Read only whitelisted keys from ``/proc/<pid>/environ``.

    Returns ``None`` when the environment cannot be read, is larger than the
    cap, or the process is gone.  Nothing outside ``keys`` is retained or
    returned, so unrelated credentials in the compositor's environment never
    reach the caller.
    """
    wanted = {key.encode(): None for key in keys}
    path = Path(proc_root) / str(pid) / "environ"
    try:
        with open(path, "rb") as stream:
            blob = stream.read(MAX_ENVIRON_BYTES + 1)
    except OSError:
        return None
    if len(blob) > MAX_ENVIRON_BYTES:
        return None
    found = {}
    for entry in blob.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if separator and name in wanted and name.decode() not in found:
            found[name.decode()] = value.decode("utf-8", "replace")
    return found


def _proc_uid(proc_root: Path, pid: int) -> Optional[int]:
    try:
        text = (Path(proc_root) / str(pid) / "status").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _proc_comm(proc_root: Path, pid: int) -> str:
    try:
        return (Path(proc_root) / str(pid) / "comm").read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_maps(proc_root: Path, pid: int) -> Optional[list]:
    try:
        with open(Path(proc_root) / str(pid) / "maps", "rb") as stream:
            blob = stream.read(MAX_MAPS_BYTES + 1)
    except OSError:
        return None
    if len(blob) > MAX_MAPS_BYTES:
        return None
    entries = []
    for raw in blob.decode("utf-8", "replace").splitlines():
        parts = raw.split(None, 5)
        if len(parts) < 6:
            continue
        _, _, _, device, inode, pathname = parts
        if not inode.isdigit() or int(inode) == 0:
            continue
        deleted = pathname.endswith(" (deleted)")
        if deleted:
            pathname = pathname[: -len(" (deleted)")]
        entries.append(_Map(device=device, inode=int(inode), path=pathname, deleted=deleted))
    return entries


@dataclass(frozen=True)
class _Map:
    device: str
    inode: int
    path: str
    deleted: bool


# --------------------------------------------------------------------------
# runtime manifest
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Library:
    rel: str
    path: str
    device: str
    inode: int
    size: int


@dataclass(frozen=True)
class RuntimeInfo:
    deployed: bool
    root: str = ""
    manifest: str = ""
    runtime_id: str = ""
    reason: str = ""
    packages: tuple = field(default_factory=tuple)
    libraries: tuple = field(default_factory=tuple)
    launcher: str = ""


def _manifest_path(environ: Mapping[str, str]) -> tuple:
    override = (environ.get(RUNTIME_ENVIRON) or "").strip()
    if override:
        if not os.path.isabs(override):
            return None, f"{RUNTIME_ENVIRON} must be an absolute path"
        return Path(override), ""
    return Path(DEFAULT_MANIFEST), ""


def _object(pairs):
    """``json`` object hook that refuses a repeated field."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid("the runtime manifest repeats a field")
        result[key] = value
    return result


def runtime_identifier(document: Mapping) -> str:
    """Canonical content-bound identifier, identical to the native stager's."""
    content = {key: value for key, value in document.items() if key != "runtime_id"}
    digest = sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    return f"{document['source_version']}.socket-stream.{digest}"


def load_runtime(environ: Mapping[str, str], *, owner_uid: int = 0) -> RuntimeInfo:
    """Resolve and validate the runtime manifest.  Never raises.

    ``owner_uid`` exists for tests only.  Production callers keep the default
    ``0``, which requires a root-owned runtime exactly like the native
    validator that runs at login.
    """
    candidate, problem = _manifest_path(environ)
    if candidate is None:
        return RuntimeInfo(False, reason=problem)
    addressed = candidate.expanduser()
    try:
        resolved = addressed.resolve(strict=True)
    except (OSError, RuntimeError):
        return RuntimeInfo(
            False, reason=f"the custom Mutter runtime is not installed at {addressed}",
        )
    if resolved.name != "runtime.json":
        return RuntimeInfo(False, reason="the custom Mutter runtime manifest is not runtime.json")

    root = resolved.parent
    info = _regular(resolved, owner_uid)
    if info is None:
        return RuntimeInfo(
            False, root=str(root), manifest=str(resolved),
            reason="the custom Mutter runtime manifest has unsafe ownership, permissions, or type",
        )
    if info.st_size > MAX_MANIFEST_BYTES:
        return RuntimeInfo(
            False, root=str(root), manifest=str(resolved),
            reason="the custom Mutter runtime manifest is too large",
        )

    def fail(reason: str) -> RuntimeInfo:
        return RuntimeInfo(
            False, root=str(root), manifest=str(resolved), reason=reason,
        )

    if not _trusted_directory(root, owner_uid):
        return fail("the custom Mutter runtime directory has unsafe ownership or permissions")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        return fail(f"the custom Mutter runtime manifest could not be read: {_error_name(exc)}")
    try:
        document = json.loads(payload, object_pairs_hook=_object)
    except _Invalid:
        return fail("the custom Mutter runtime manifest repeats a field")
    except (ValueError, UnicodeDecodeError):
        return fail("the custom Mutter runtime manifest is not valid JSON")
    if not isinstance(document, dict):
        return fail("the custom Mutter runtime manifest is not a JSON object")

    # --- schema and identity, exactly as the native validator ------------
    schema = document.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        if schema == 1:
            return fail(
                "the custom Mutter runtime uses the superseded schema 1; rebuild it")
        return fail("unsupported custom Mutter runtime manifest schema")
    if document.get("shell") != SHELL:
        return fail("the custom Mutter runtime does not pin the GNOME Shell executable")
    runtime_id = document.get("runtime_id")
    if not isinstance(runtime_id, str) or not IDENTIFIER.fullmatch(runtime_id):
        return fail("the custom Mutter runtime identifier is invalid")
    source_version = document.get("source_version")
    if not isinstance(source_version, str) or not VERSION.fullmatch(source_version):
        return fail("the custom Mutter runtime source version is invalid")
    for key in ("source_sha256", "patch_sha256"):
        value = document.get(key)
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            return fail("the custom Mutter runtime provenance is incomplete")

    # --- loader layout ---------------------------------------------------
    libraries = document.get("libraries")
    if (document.get("library_dir") != "lib"
            or document.get("typelib_dir") != "typelib"
            or not isinstance(libraries, list)
            or sorted(libraries, key=str) != sorted(LIBRARIES)):
        return fail("unsupported custom Mutter library layout")

    # --- package contract -------------------------------------------------
    packages = document.get("packages")
    if (not isinstance(packages, dict) or set(packages) != set(REQUIRED_PACKAGES)
            or any(not isinstance(value, str) or not VERSION.fullmatch(value)
                   for value in packages.values())):
        return fail("the custom Mutter runtime package contract is incomplete")
    if any(
        packages[name] != source_version
        for name in REQUIRED_PACKAGES if "gnome-shell" not in name
    ):
        return fail("the custom Mutter runtime source and package versions differ")

    # --- file set ---------------------------------------------------------
    files = document.get("files")
    required = set(LIBRARIES + TYPELIBS + SCRIPTS + (SHELL,))
    if (not isinstance(files, dict) or not required <= files.keys()
            or files.keys() - required - set(PROVENANCE)):
        return fail("the custom Mutter runtime file set is incomplete or unexpected")

    # --- loader directories must contain exactly their entries ------------
    for directory, entries in (("lib", LIBRARIES), ("typelib", TYPELIBS)):
        location = root / directory
        if not _trusted_directory(location, owner_uid):
            return fail(f"the custom Mutter runtime {directory}/ has unsafe ownership or permissions")
        try:
            names = {entry.name for entry in location.iterdir()}
        except OSError:
            return fail(f"the custom Mutter runtime has no {directory}/ directory")
        if names != {Path(name).name for name in entries}:
            return fail(f"the custom Mutter runtime {directory}/ contains unexpected files")

    # --- payloads ---------------------------------------------------------
    sizes = {}
    for name, expected in files.items():
        if (not isinstance(expected, dict) or set(expected) != {"sha256", "size"}
                or not isinstance(expected["sha256"], str)
                or not SHA256.fullmatch(expected["sha256"])
                or type(expected["size"]) is not int):
            return fail(f"the custom Mutter runtime file metadata for {name} is invalid")
        digest, size, problem = _file_digest(root / name, owner_uid)
        if problem:
            return fail(f"the custom Mutter runtime file {name} {problem}")
        if size != expected["size"] or digest != expected["sha256"]:
            return fail(f"the custom Mutter runtime file {name} does not match its manifest")
        sizes[name] = size

    launcher = root / "launch-gnome-shell"
    launcher_info = _regular(launcher, owner_uid)
    if launcher_info is None:
        return fail("the custom Mutter runtime launcher has unsafe ownership, permissions, or type")
    for name in EXECUTABLES:
        entry_info = _regular(root / name, owner_uid)
        if entry_info is None:
            return fail(
                f"the custom Mutter runtime {name} has unsafe ownership, permissions, or type")
        if not entry_info.st_mode & stat.S_IXUSR:
            return fail(f"the custom Mutter runtime {name} is not executable")

    for filename, key in (("source.dsc", "source_sha256"),
                          ("capture.patch", "patch_sha256")):
        if filename in files and files[filename]["sha256"] != document[key]:
            return fail("the custom Mutter runtime provenance hash does not match")

    if runtime_id != runtime_identifier(document):
        return fail("the custom Mutter runtime identifier does not match its manifest")
    try:
        if _stamp(resolved.lstat()) != _stamp(info):
            return fail("the custom Mutter runtime manifest changed while it was read")
    except OSError:
        return fail("the custom Mutter runtime manifest changed while it was read")

    resolved_libraries = []
    for rel in LIBRARIES:
        path = root / rel
        try:
            entry_info = path.lstat()
        except OSError:
            return fail(f"the custom Mutter runtime library {rel} is missing")
        resolved_libraries.append(Library(
            rel=rel, path=os.path.realpath(str(path)),
            device=_dev_text(entry_info.st_dev), inode=entry_info.st_ino,
            size=sizes[rel],
        ))

    return RuntimeInfo(
        True, root=str(root), manifest=str(resolved), runtime_id=runtime_id,
        packages=tuple(sorted(packages.items())),
        libraries=tuple(resolved_libraries),
        launcher=str(launcher),
    )


def packages_match(packages: Iterable, *, runner: Runner = None,
                   which: Which = None) -> tuple:
    """Compare the manifest's pinned packages with the installed versions.

    Returns ``(True, "")`` when everything matches, ``(False, reason)`` on a
    version mismatch, and ``(None, reason)`` when the installed versions cannot
    be established (callers must treat that as incompatible, never as success).
    Mirrors the native validator: the query must report the exact status
    ``install ok installed`` for every requested package, and a duplicate or
    unexpected package line is a failure, not something to average over.
    """
    runner = runner or _run
    which = which or shutil.which
    wanted = dict(packages)
    if not wanted:
        return None, "the custom Mutter runtime pins no packages"
    tool = which("dpkg-query")
    if not tool:
        return None, "dpkg-query is unavailable, so the installed package versions cannot be read"
    names = sorted(wanted)
    try:
        result = runner(
            [tool, "-W", "-f=${Package}\t${Version}\t${Status}\n", *names],
            15, {"LC_ALL": "C"},
        )
    except Exception:  # noqa: BLE001 - a probe must never propagate
        return None, "the installed package versions could not be read"
    if _returncode(result) != 0:
        return None, "dpkg-query reported an error reading installed package versions"
    found = {}
    for line in (getattr(result, "stdout", "") or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if (len(fields) != 3 or fields[0] not in wanted or fields[0] in found
                or fields[2] != "install ok installed"
                or not VERSION.fullmatch(fields[1])):
            return None, "the installed package status for the custom runtime is unknown"
        found[fields[0]] = fields[1]
    if set(found) != set(wanted):
        return None, "the installed package set for the custom runtime is incomplete"
    for name in names:
        if found[name] != wanted[name]:
            return False, f"{name} is {found[name]}, but the runtime was built for {wanted[name]}"
    return True, ""


# --------------------------------------------------------------------------
# running shell + unit detection
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ShellInfo:
    ok: bool
    pid: int = 0
    unit: str = ""
    mode: str = ""
    source: str = ""
    reason: str = ""
    # "stock" (the unit's own ExecStart) or "ours" (a managed override is in
    # effect).  ``managed_root`` is the runtime directory that override names,
    # ``guarded_state`` the absolute guard state path a schema-2 override pins,
    # and the two post properties carry the effective lifecycle hooks.
    exec_kind: str = ""
    managed_root: str = ""
    guarded_state: str = ""
    exec_start_post: str = ""
    exec_stop_post: str = ""


def _dbus_shell_pid(runner: Runner, which: Which) -> tuple:
    tool = which("gdbus")
    if not tool:
        return None, "gdbus is not available"
    try:
        result = runner([
            tool, "call", "--session",
            "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID",
            "org.gnome.Shell",
        ], 15)
    except Exception:  # noqa: BLE001
        return None, "the session bus did not answer"
    if _returncode(result) != 0:
        return None, "the session bus has no org.gnome.Shell name"
    match = re.search(r"uint32\s+(\d+)", _text(result))
    if not match:
        return None, "the session bus returned an unexpected answer"
    return int(match.group(1)), ""


def _scan_shell_pids(proc_root: Path, uid: int, session_id: str) -> tuple:
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return None, "the process table is not readable"
    candidates = []
    for entry in entries[:MAX_PROC_ENTRIES]:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        if _proc_comm(proc_root, pid) != "gnome-shell":
            continue
        if _proc_uid(proc_root, pid) != uid:
            continue
        if session_id:
            environ = _read_environ(proc_root, pid, ("XDG_SESSION_ID",))
            if not environ or environ.get("XDG_SESSION_ID") != session_id:
                continue
        candidates.append(pid)
    if not candidates:
        return None, "no GNOME Shell process was found for this session"
    if len(candidates) > 1:
        return None, "several GNOME Shell processes matched this session"
    return candidates[0], ""


def _unit_from_cgroup(proc_root: Path, pid: int) -> str:
    try:
        text = (Path(proc_root) / str(pid) / "cgroup").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        _hierarchy, _separator, path = line.partition(":")
        _controllers, _separator, path = path.partition(":")
        for component in reversed([part for part in path.split("/") if part]):
            if component.endswith(".service") or component.endswith(".scope"):
                return component
    return ""


def _systemctl_show(runner: Runner, which: Which, unit: str) -> Optional[dict]:
    tool = which("systemctl")
    if not tool:
        return None
    try:
        result = runner([
            tool, "--user", "show", unit,
            "-p", "MainPID", "-p", "LoadState", "-p", "ExecStart", "-p", "FragmentPath",
            "-p", "ExecStartPost", "-p", "ExecStopPost", "-p", "Type", "-p", "NotifyAccess",
        ], 15)
    except Exception:  # noqa: BLE001
        return None
    if _returncode(result) != 0:
        return None
    properties = {}
    for line in (getattr(result, "stdout", "") or "").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            properties[name.strip()] = value.strip()
    return properties


# Two script bodies, shared by the renderer, the recogniser and the ExecStart
# classifier so they can never drift apart.  The legacy body is schema 1 and is
# still recognised and removable; the guarded body is schema 2 and pins the
# absolute guard state path.
_LEGACY_SCRIPT_BODY = (
    r"if test -x (?P<root>/[^\s';]+)/launch-gnome-shell; then exec "
    r"(?P=root)/launch-gnome-shell --mode=(?P<mode>[A-Za-z0-9_.\-]+); fi; exec "
    r"/usr/bin/gnome-shell --mode=(?P=mode)\Z"
)
_GUARDED_SCRIPT_BODY = (
    r"if test -x (?P<root>/[^\s';]+)/launch-gnome-shell; then exec "
    r"(?P=root)/launch-gnome-shell --guard-state=(?P<state>/[^\s';]+) "
    r"--mode=(?P<mode>[A-Za-z0-9_.\-]+); fi; exec "
    r"/usr/bin/gnome-shell --mode=(?P=mode)\Z"
)
_MANAGED_SCRIPT_LEGACY = re.compile(r"\A" + _LEGACY_SCRIPT_BODY)
_MANAGED_SCRIPT_GUARDED = re.compile(r"\A" + _GUARDED_SCRIPT_BODY)
_MANAGED_EXEC_ARGV_LEGACY = re.compile(r"\A/bin/sh -c " + _LEGACY_SCRIPT_BODY)
_MANAGED_EXEC_ARGV_GUARDED = re.compile(r"\A/bin/sh -c " + _GUARDED_SCRIPT_BODY)
_MANAGED_EXEC_LINE = re.compile(r"^ExecStart=/bin/sh -c '([^']*)'$", re.MULTILINE)
_STOCK_ARGV = re.compile(r"\A/usr/bin/gnome-shell --mode=(?P<mode>[A-Za-z0-9_.\-]+)\Z")
# One ``argv[]=...`` segment of a systemd Exec* property value.
_EXEC_ARGV = re.compile(r"argv\[\]=(.*?)(?:\s;\s|\s*\}\s*$)")


def managed_script(root: str, mode: str, state: str) -> str:
    launcher = f"{root}/launch-gnome-shell"
    return (
        f"if test -x {launcher}; then exec {launcher} "
        f"--guard-state={state} --mode={mode}; fi; "
        f"exec {STOCK_SHELL} --mode={mode}"
    )


def _legacy_script(root: str, mode: str) -> str:
    launcher = f"{root}/launch-gnome-shell"
    return (
        f"if test -x {launcher}; then exec {launcher} --mode={mode}; fi; "
        f"exec {STOCK_SHELL} --mode={mode}"
    )


def parse_exec_start(value: str) -> tuple:
    """Classify ``systemctl show -p ExecStart``: ``(kind, root, mode)``.

    ``kind`` is ``"stock"`` (the unit's own line), ``"ours"`` (a managed
    override, either schema 1 or schema 2), or ``""`` for anything unrecognised;
    callers must treat ``""`` as a refusal rather than guessing.  The guard
    state path is read separately with :func:`guarded_state` so this tuple stays
    compatible with the schema-1 callers.
    """
    if not value or value.count("{ path=") != 1 or value.count("argv[]=") != 1:
        return "", "", ""
    match = re.search(r"argv\[\]=(.*?)(?:\s;\s|\s*$)", value)
    if not match:
        return "", "", ""
    argv = match.group(1).strip()
    stock = _STOCK_ARGV.match(argv)
    if stock and value.startswith("{ path=/usr/bin/gnome-shell ;"):
        return "stock", "", stock.group("mode")
    for pattern in (_MANAGED_EXEC_ARGV_GUARDED, _MANAGED_EXEC_ARGV_LEGACY):
        ours = pattern.match(argv)
        if ours and value.startswith("{ path=/bin/sh ;"):
            return "ours", ours.group("root"), ours.group("mode")
    return "", "", ""


def guarded_state(value: str) -> str:
    """The absolute guard state path a schema-2 managed ``ExecStart`` names.

    Returns ``""`` for a stock line, a schema-1 override, or anything the
    classifier does not recognise verbatim.
    """
    if not value or value.count("{ path=") != 1 or value.count("argv[]=") != 1:
        return ""
    match = re.search(r"argv\[\]=(.*?)(?:\s;\s|\s*$)", value)
    if not match:
        return ""
    ours = _MANAGED_EXEC_ARGV_GUARDED.match(match.group(1).strip())
    return ours.group("state") if ours else ""


def exec_kind(value: str, mode: str = "") -> str:
    kind, _root, found = parse_exec_start(value)
    if kind and mode and found != mode:
        return ""
    return kind


def managed_root(value: str, mode: str = "") -> str:
    """The runtime directory a managed ``ExecStart`` names, or ``""``."""
    kind, root, found = parse_exec_start(value)
    if kind != "ours" or (mode and found != mode):
        return ""
    return root


def detect_shell(*, runner: Runner = None, which: Which = None,
                 proc_root: Optional[Path] = None, uid: Optional[int] = None,
                 environ: Optional[Mapping[str, str]] = None) -> ShellInfo:
    """Identify the systemd unit that runs the GNOME Shell of this session."""
    runner = runner or _run
    which = which or shutil.which
    proc_root = Path(proc_root) if proc_root is not None else Path("/proc")
    uid = os.getuid() if uid is None else int(uid)
    environ = dict(os.environ if environ is None else environ)

    pid, reason = _dbus_shell_pid(runner, which)
    source = "dbus"
    if not pid:
        session_id = (environ.get("XDG_SESSION_ID") or "").strip()
        pid, scan_reason = _scan_shell_pids(proc_root, uid, session_id)
        source = "proc"
        if not pid:
            # The process-table answer is the more specific one; the bus
            # message only explains why the fallback was needed.
            return ShellInfo(False, reason=scan_reason or reason)

    if _proc_uid(proc_root, pid) != uid:
        return ShellInfo(
            False, pid=pid,
            reason="the GNOME Shell process is not owned by this user",
        )
    if _proc_comm(proc_root, pid) != "gnome-shell":
        return ShellInfo(
            False, pid=pid,
            reason="the process named by the session bus is not GNOME Shell",
        )
    unit = _unit_from_cgroup(proc_root, pid)
    if not unit:
        return ShellInfo(
            False, pid=pid,
            reason="the GNOME Shell process is not in a systemd unit",
        )
    match = UNIT_PATTERN.match(unit)
    if not match:
        return ShellInfo(
            False, pid=pid, unit=unit,
            reason=f"unfamiliar GNOME Shell unit {unit}",
        )
    mode = match.group(1)
    if not MODE_PATTERN.match(mode):
        return ShellInfo(False, pid=pid, unit=unit,
                         reason=f"unfamiliar GNOME Shell mode {mode!r}")

    properties = _systemctl_show(runner, which, unit)
    if properties is None:
        return ShellInfo(
            False, pid=pid, unit=unit, mode=mode,
            reason="systemctl could not describe the GNOME Shell user service",
        )
    if properties.get("LoadState") != "loaded":
        return ShellInfo(
            False, pid=pid, unit=unit, mode=mode,
            reason=f"the GNOME Shell user service is {properties.get('LoadState') or 'unknown'}",
        )
    if properties.get("Type") != "notify" or properties.get("NotifyAccess") != "main":
        return ShellInfo(False, pid=pid, unit=unit, mode=mode,
                         reason="the GNOME Shell service must use Type=notify and NotifyAccess=main")
    main_pid = properties.get("MainPID", "")
    if not main_pid.isdigit() or int(main_pid) != pid:
        return ShellInfo(
            False, pid=pid, unit=unit, mode=mode,
            reason="the GNOME Shell user service does not run this shell process",
        )
    exec_start = properties.get("ExecStart", "")
    kind = exec_kind(exec_start, mode)
    if kind not in ("stock", "ours"):
        return ShellInfo(
            False, pid=pid, unit=unit, mode=mode,
            reason="the GNOME Shell user service uses an unfamiliar ExecStart",
        )
    return ShellInfo(
        True, pid=pid, unit=unit, mode=mode, source=source,
        exec_kind=kind, managed_root=managed_root(exec_start, mode),
        guarded_state=guarded_state(exec_start),
        exec_start_post=properties.get("ExecStartPost", ""),
        exec_stop_post=properties.get("ExecStopPost", ""),
    )


# --------------------------------------------------------------------------
# live evidence
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveState:
    state: str
    opt_in: Optional[bool] = None
    detail: str = ""

    @property
    def active(self) -> bool:
        return self.state == "active"


def _is_mutter(name: str) -> bool:
    return name.startswith(MUTTER_LIBRARY_PREFIX) and name != MUTTER_LIBRARY_PREFIX


def read_live_state(shell: ShellInfo, runtime: RuntimeInfo,
                    proc_root: Optional[Path] = None) -> LiveState:
    """Classify the running compositor against the validated runtime.

    Every Mutter-family mapping is grouped by file name.  Activation requires
    all four expected files, every segment of a name to agree, a device/inode
    match with the validated files, no ``(deleted)`` segment, and no
    unexpected family member from elsewhere.
    """
    proc_root = Path(proc_root) if proc_root is not None else Path("/proc")
    if not shell.ok:
        return LiveState("unknown", detail="the GNOME Shell process is not identified")
    if not runtime.deployed:
        return LiveState("unknown", detail="the custom runtime is not deployed")
    environ = _read_environ(proc_root, shell.pid)
    if environ is None:
        return LiveState(
            "unknown",
            detail="the compositor environment could not be read, so activation is unknown",
        )
    opt_in = environ.get(OPT_IN_ENVIRON) == "1"
    entries = _read_maps(proc_root, shell.pid)
    if entries is None:
        return LiveState(
            "unknown", opt_in,
            "the compositor memory map could not be read, so activation is unknown",
        )

    library_dir = os.path.realpath(str(Path(runtime.root) / "lib"))
    expected = {}
    for library in runtime.libraries:
        expected.setdefault(os.path.basename(library.path), set()).add(
            (library.device, library.inode))

    family = {}
    for entry in entries:
        base = os.path.basename(entry.path)
        if _is_mutter(base):
            family.setdefault(base, []).append(entry)

    unexpected = sorted(name for name in family if name not in expected)
    if unexpected:
        return LiveState(
            "mixed", opt_in,
            f"GNOME Shell has {unexpected[0]} mapped, which is not part of the "
            "validated runtime; log out and back in",
        )
    deleted = sorted(name for name, parts in family.items()
                     if any(segment.deleted for segment in parts))
    if deleted:
        return LiveState(
            "stale", opt_in,
            f"GNOME Shell is using {deleted[0]} from a runtime build that was removed "
            "from disk; log out and back in",
        )

    def inside(entry) -> bool:
        return os.path.realpath(os.path.dirname(entry.path)) == library_dir

    loaded = {name: [entry for entry in parts if inside(entry)]
              for name, parts in family.items()}
    if family and not any(loaded.values()):
        where = os.path.dirname(next(iter(family.values()))[0].path)
        return LiveState(
            "stock", opt_in,
            f"GNOME Shell is using the system Mutter libraries from {where}, "
            "not the custom runtime",
        )
    split = sorted(name for name, parts in loaded.items()
                   if parts and len(parts) != len(family[name]))
    if split:
        return LiveState(
            "mixed", opt_in,
            f"{split[0]} is mapped from more than one directory; this is unsupported. "
            "Log out and back in to reload the runtime",
        )
    outside = sorted(name for name, parts in loaded.items() if not parts and family[name])
    if outside:
        return LiveState(
            "mixed", opt_in,
            f"{outside[0]} is loaded from outside the validated runtime while other "
            "Mutter libraries come from it; this is unsupported. Log out and back in "
            "to reload the runtime",
        )
    missing = sorted(set(expected) - set(family))
    if missing:
        return LiveState(
            "missing", opt_in,
            f"GNOME Shell does not have {missing[0]} loaded from the custom runtime",
        )
    mismatched = sorted(
        name for name, parts in family.items()
        if any((segment.device, segment.inode) not in expected[name] for segment in parts)
    )
    if mismatched:
        return LiveState(
            "stale", opt_in,
            f"GNOME Shell is using a different {mismatched[0]} build than the installed "
            "files; log out and back in",
        )
    if not opt_in:
        return LiveState(
            "inactive", opt_in,
            "the custom runtime is loaded, but the compositor does not have "
            f"{OPT_IN_ENVIRON}=1",
        )
    return LiveState("active", True, "the custom runtime is loaded and the opt-in is set")


# --------------------------------------------------------------------------
# managed activation files
# --------------------------------------------------------------------------
def dropin_dir(config_home: Path, unit: str) -> Path:
    return Path(config_home) / "systemd" / "user" / f"{unit}.d"


def dropin_path(config_home: Path, unit: str) -> Path:
    return dropin_dir(config_home, unit) / MANAGED_FILENAME


# --------------------------------------------------------------------------
# startup guard: state path, safe reader, and the helper commands
# --------------------------------------------------------------------------
def guard_state_path(config_home: Path, root: str, mode: str) -> Path:
    """Deterministic guard state path, pinned absolutely in the override.

    The digest binds the runtime directory and the session mode, so a state
    record can never be confused between runtimes or sessions.  The override
    always names the resolved absolute path; the helper never inherits a
    different ``XDG_CONFIG_HOME`` at login.
    """
    digest = sha256((root + "\0" + mode).encode()).hexdigest()[:32]
    return Path(config_home).joinpath(*GUARD_STATE_PARTS, f"{digest}.json")


@dataclass(frozen=True)
class GuardState:
    """The guard's own record, read without ever executing the helper.

    Only ``armed`` permits the next custom launch. ``pending`` and ``ready``
    describe a consumed attempt, not proof that its process is still running.
    """

    phase: str
    runtime: str = ""
    mode: str = ""
    detail: str = ""
    invocation_id: str = ""
    boot_id: str = ""
    pid: int = 0
    starttime: int = 0

    @property
    def armed(self) -> bool:
        return self.phase == "armed"

    @property
    def blocked(self) -> bool:
        return self.phase == "blocked"

    @property
    def missing(self) -> bool:
        return self.phase == GUARD_MISSING


def _guard_record(document) -> bool:
    if (not isinstance(document, dict) or type(document.get("schema")) is not int
            or document["schema"] != 1
            or set(document) - {"schema", "runtime", "mode", "phase", "reason", *GUARD_IDENTITY}
            or document.get("phase") not in GUARD_PHASES
            or not isinstance(document.get("runtime"), str)
            or not SAFE_PATH_PATTERN.fullmatch(document["runtime"])
            or ".." in Path(document["runtime"]).parts
            or not IDENTIFIER.fullmatch(Path(document["runtime"]).name)
            or not isinstance(document.get("mode"), str)
            or not MODE_PATTERN.fullmatch(document["mode"])
            or ("reason" in document and document["reason"] not in GUARD_REASONS)):
        return False
    present = set(GUARD_IDENTITY) & document.keys()
    if present and present != set(GUARD_IDENTITY):
        return False
    if document["phase"] in ("pending", "ready") and not present:
        return False
    if document["phase"] == "armed" and present:
        return False
    return not present or (
        isinstance(document["invocation_id"], str) and INVOCATION.fullmatch(document["invocation_id"])
        and isinstance(document["boot_id"], str) and BOOT_ID.fullmatch(document["boot_id"])
        and type(document["pid"]) is int and document["pid"] > 0
        and type(document["starttime"]) is int and document["starttime"] > 0
    )


def read_guard_state(path: Path, *, root: str = "", mode: str = "") -> GuardState:
    """Read a bounded guard JSON record; never execute a helper.

    A symlink, an oversized or unreadable file, invalid JSON, or a record that
    does not carry the schema-1 fields is reported as :data:`GUARD_MISSING`
    rather than guessed at.
    """
    path = Path(path)
    try:
        with _user_directory(path.parent, private=True) as directory:
            blob = _read_user_file(directory, path.name, MAX_GUARD_BYTES, private=True)
        if blob is None:
            return GuardState(GUARD_MISSING)
        document = json.loads(blob.decode("ascii"), object_pairs_hook=_object)
        if not _guard_record(document):
            return GuardState(GUARD_MISSING, detail="the startup guard record is invalid")
        if (root and document["runtime"] != root) or (mode and document["mode"] != mode):
            return GuardState(GUARD_MISSING, detail="the startup guard belongs to another runtime or mode")
        return GuardState(document["phase"], document["runtime"], document["mode"],
                          **{key: document[key] for key in GUARD_IDENTITY if key in document})
    except (OSError, MutterCaptureError, _Invalid, ValueError, UnicodeDecodeError):
        return GuardState(GUARD_MISSING, detail="the startup guard could not be read safely")


def guard_matches_running(guard: GuardState, shell: ShellInfo, runtime: RuntimeInfo,
                          proc_root: Optional[Path] = None) -> bool:
    """A consumed record is current only with matching boot, PID, exe and env."""
    if (guard.phase not in ("pending", "ready") or not shell.ok or not runtime.deployed
            or guard.runtime != runtime.root or guard.mode != shell.mode or guard.pid != shell.pid):
        return False
    proc = Path("/proc") if proc_root is None else Path(proc_root)
    try:
        boot = (proc / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        fields = (proc / str(shell.pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if (boot != guard.boot_id or fields[0] == "Z" or int(fields[19]) != guard.starttime
                or not os.path.samefile(proc / str(shell.pid) / "exe", Path(runtime.root) / SHELL)):
            return False
    except (OSError, ValueError, IndexError):
        return False
    environ = _read_environ(proc, shell.pid, (OPT_IN_ENVIRON, "INVOCATION_ID"))
    return bool(environ and environ.get(OPT_IN_ENVIRON) == "1"
                and environ.get("INVOCATION_ID") == guard.invocation_id)


def guard_command(root: str, mode: str, state: str, action: str) -> list:
    """The exact argv the override runs for one guard action."""
    return [
        "/usr/bin/python3", "-I", f"{root}/startup_guard.py", action,
        "--runtime", root, "--mode", mode, "--state", state,
    ]


def probe_command(root: str, mode: str) -> list:
    """The exact argv of the isolated startup probe."""
    return [
        "/usr/bin/python3", "-I", f"{root}/startup_probe.py",
        "--runtime", root, "--mode", mode,
    ]


def _exec_argv(value: str) -> list:
    """Every ``argv[]=...`` command in a systemd Exec* property value."""
    return [match.group(1).strip() for match in _EXEC_ARGV.finditer(value or "")]


def _hook_present(value: str, argv: list) -> bool:
    """True when ``argv`` appears exactly once in an Exec* property value.

    Upstream may contribute its own ``ExecStopPost``; only an exact, single
    match of our own command counts, so a duplicate or a lookalike is refused.
    """
    wanted = " ".join(argv)
    commands = _exec_argv(value)
    # An extra guard hook for another root/state is ambiguous too. Upstream's
    # unrelated ExecStopPost (display-variable cleanup) remains allowed.
    guards = [command for command in commands if "/startup_guard.py" in command]
    return (guards == [wanted]
            and f"{{ path={argv[0]} ; argv[]={wanted} ; ignore_errors=yes ;" in value)


def own_post_hooks(*, exec_start_post: str, exec_stop_post: str,
                   root: str, mode: str, state: str) -> bool:
    """Both lifecycle hooks are present and unambiguous in the effective unit."""
    return (
        _hook_present(exec_start_post, guard_command(root, mode, state, "ready"))
        and _hook_present(exec_stop_post, guard_command(root, mode, state, "finish"))
    )


def _override_effective(properties: Optional[dict], root: str, mode: str, state: str) -> bool:
    """The unit's effective configuration is exactly this schema-2 override."""
    if (not properties or properties.get("LoadState") != "loaded"
            or properties.get("Type") != "notify" or properties.get("NotifyAccess") != "main"):
        return False
    exec_start = properties.get("ExecStart", "")
    if exec_kind(exec_start, mode) != "ours" or managed_root(exec_start, mode) != root:
        return False
    if guarded_state(exec_start) != state:
        return False
    return own_post_hooks(
        exec_start_post=properties.get("ExecStartPost", ""),
        exec_stop_post=properties.get("ExecStopPost", ""),
        root=root, mode=mode, state=state,
    )


def run_startup_probe(root: str, mode: str, *, runner: Runner,
                      timeout: int = PROBE_TIMEOUT) -> tuple:
    """Run the isolated private-Shell startup check.  Returns ``(ok, detail)``.

    The probe is the only thing allowed to start a private Shell, and it never
    arms the guard.  A missing tool, a non-zero exit, or a result that does not
    carry ``ok: true`` is a failure the caller must treat as "do not install".
    """
    try:
        result = runner(probe_command(root, mode), timeout)
    except Exception as exc:  # noqa: BLE001 - a probe must never propagate
        return False, f"the isolated startup check could not run: {_error_name(exc)}"
    if _returncode(result) != 0:
        return False, "the isolated startup check did not reach a private Shell READY state"
    try:
        document = json.loads((getattr(result, "stdout", "") or "").strip())
    except (ValueError, TypeError):
        return False, "the isolated startup check returned no result"
    if not isinstance(document, dict) or document.get("ok") is not True:
        return False, "the isolated startup check did not confirm a private Shell READY state"
    return True, ""


def _guard_request(root: str, mode: str, state: str, action: str,
                   runner: Runner, timeout: int = GUARD_TIMEOUT) -> dict:
    try:
        result = runner(guard_command(root, mode, state, action), timeout)
    except Exception as exc:  # noqa: BLE001
        raise MutterCaptureError(
            f"the startup guard could not run {action}: {_error_name(exc)}") from None
    if _returncode(result) != 0:
        raise MutterCaptureError(f"the startup guard refused to {action}")
    try:
        document = json.loads((getattr(result, "stdout", "") or "").strip())
    except (ValueError, TypeError):
        raise MutterCaptureError(f"the startup guard {action} returned no result") from None
    if not isinstance(document, dict):
        raise MutterCaptureError(f"the startup guard {action} returned no result")
    return document


def arm_guard(root: str, mode: str, state: str, runner: Runner,
               timeout: int = GUARD_TIMEOUT) -> str:
    """Only a matching durable armed record is successful explicit arming."""
    document = _guard_request(root, mode, state, "arm", runner, timeout)
    if (not _guard_record(document) or document.get("phase") != "armed"
            or document.get("runtime") != root or document.get("mode") != mode
            or not read_guard_state(Path(state), root=root, mode=mode).armed):
        raise MutterCaptureError("the startup guard did not arm the activation")
    return "armed"


def _revoke_guard(config_home: Path, root: str, mode: str, state: str) -> None:
    """Disarm a schema-2 guard before its override is removed.

    Uses the native helper's stable nonblocking lock and durable blocked-record
    protocol locally. Removal never executes an unvalidated or vanished runtime.
    """
    import fcntl

    expected = guard_state_path(config_home, root, mode)
    if str(expected) != state:
        raise MutterCaptureError("the override pins an unexpected startup guard path; it was left untouched")
    payload = (json.dumps({"schema": 1, "runtime": root, "mode": mode,
                           "phase": "blocked", "reason": "disarmed"}, sort_keys=True) + "\n").encode()
    try:
        with _user_directory(expected.parent, create=True, private=True) as directory:
            name = expected.name + ".lock"
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            created = False
            try:
                lock = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                created = True
            except FileExistsError:
                lock = os.open(name, flags, dir_fd=directory)
            try:
                info = os.fstat(lock)
                if created and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                    os.fchmod(lock, 0o600)
                    info = os.fstat(lock)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise MutterCaptureError("the startup guard lock has unsafe ownership, permissions, or type")
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                previous = _read_user_file(directory, expected.name, MAX_GUARD_BYTES, private=True)
                # A corrupt owned guard is safe to revoke at its exact generated
                # path. A valid record for a different identity is preserved.
                if previous:
                    try:
                        document = json.loads(previous, object_pairs_hook=_object)
                    except (ValueError, _Invalid):
                        document = None
                    if (_guard_record(document)
                            and (document["runtime"], document["mode"]) != (root, mode)):
                        raise MutterCaptureError("the startup guard belongs to another runtime or mode")
                _replace_user_file(directory, expected.name, payload, expected=previous,
                                   limit=MAX_GUARD_BYTES, private=True)
            finally:
                os.close(lock)
    except OSError as exc:
        raise MutterCaptureError(f"the startup guard could not be revoked: {_error_name(exc)}") from None


def render_dropin(root: str, mode: str, state: str) -> str:
    """Return the schema-2 managed unit override for one exact runtime directory.

    ``state`` is the absolute guard state path the launcher pins and the two
    lifecycle hooks address.  The override never resets the upstream unit's own
    ``ExecStopPost``, ``Type``, ``NotifyAccess``, ``Restart`` or ``OnFailure``:
    it only clears and replaces ``ExecStart`` and appends its own hooks.
    """
    if not SAFE_PATH_PATTERN.fullmatch(root or "") or ".." in Path(root).parts:
        raise MutterCaptureError("the runtime path contains characters the unit file cannot hold")
    if not MODE_PATTERN.fullmatch(mode or ""):
        raise MutterCaptureError("the GNOME Shell mode is not a plain identifier")
    if not SAFE_PATH_PATTERN.fullmatch(state or "") or ".." in Path(state).parts:
        raise MutterCaptureError("the guard state path contains characters the unit file cannot hold")
    script = managed_script(root, mode, state)
    ready = " ".join(guard_command(root, mode, state, "ready"))
    finish = " ".join(guard_command(root, mode, state, "finish"))
    return (
        f"{MANAGED_HEADER}. Remove it from the app, not by hand.\n"
        "# The launcher re-validates the custom runtime at login and falls back\n"
        "# to the stock GNOME Shell when the runtime is missing or incompatible.\n"
        "# Recovery from a TTY: delete this file, then systemctl --user daemon-reload\n"
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart=/bin/sh -c '{script}'\n"
        f"ExecStartPost=-{ready}\n"
        f"ExecStopPost=-{finish}\n"
    )


def _render_legacy_dropin(root: str, mode: str) -> str:
    """The exact schema-1 override an earlier release wrote, for recognition."""
    if not SAFE_PATH_PATTERN.match(root or "") or not MODE_PATTERN.match(mode or ""):
        raise MutterCaptureError("the legacy override cannot be rendered")
    return (
        f"{MANAGED_HEADER}. Remove it from the app, not by hand.\n"
        "# The launcher re-validates the custom runtime at login and falls back\n"
        "# to the stock GNOME Shell when the runtime is missing or incompatible.\n"
        "# Recovery from a TTY: delete this file, then systemctl --user daemon-reload\n"
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart=/bin/sh -c '{_legacy_script(root, mode)}'\n"
    )


@dataclass(frozen=True)
class ManagedOverride:
    """One recognised SocketStream override, schema 1 or schema 2.

    ``format`` is ``"guarded"`` for the schema-2 override that pins a guard
    state path, and ``"legacy"`` for the schema-1 override, whose ``state`` is
    empty.  A legacy override stays removable and migratable but never counts
    as configured.
    """

    root: str
    mode: str
    state: str = ""
    format: str = "guarded"

    @property
    def legacy(self) -> bool:
        return self.format == "legacy"


def parse_managed_content(text: str) -> Optional[ManagedOverride]:
    """Return the override ``text`` renders exactly, or ``None``.

    Equality with :func:`render_dropin` (or the schema-1 renderer) is the whole
    test, so an extra header line, a reordered section, or any other hand edit
    can never be mistaken for ours and deleted.  Both formats are recognised so
    an old activation stays removable and migratable.
    """
    if not isinstance(text, str):
        return None
    match = _MANAGED_EXEC_LINE.search(text)
    if not match:
        return None
    guarded = _MANAGED_SCRIPT_GUARDED.match(match.group(1))
    if guarded:
        root, mode, state = guarded.group("root"), guarded.group("mode"), guarded.group("state")
        try:
            if text == render_dropin(root, mode, state):
                return ManagedOverride(root, mode, state, "guarded")
        except MutterCaptureError:
            pass
    legacy = _MANAGED_SCRIPT_LEGACY.match(match.group(1))
    if legacy:
        root, mode = legacy.group("root"), legacy.group("mode")
        try:
            if text == _render_legacy_dropin(root, mode):
                return ManagedOverride(root, mode, "", "legacy")
        except MutterCaptureError:
            pass
    return None


def is_managed_content(text: str) -> bool:
    return parse_managed_content(text) is not None


@dataclass(frozen=True)
class Activation:
    """One candidate file at the managed name.

    ``regular`` is false for a symlink, a directory, a broken symlink, or an
    unreadable file.  Such an entry is always preserved and never deleted.
    """

    path: Path
    text: str = ""
    regular: bool = True

    @property
    def override(self) -> Optional[ManagedOverride]:
        parsed = parse_managed_content(self.text) if self.regular else None
        if parsed is None:
            return None
        # Format recognition alone does not authorise mutation of arbitrary
        # state paths or an override copied into another session's directory.
        if self.path.parent.name != f"org.gnome.Shell@{parsed.mode}.service.d":
            return None
        if not parsed.legacy and parsed.state != str(guard_state_path(
                self.path.parents[3], parsed.root, parsed.mode)):
            return None
        return parsed

    @property
    def managed(self) -> bool:
        return self.override is not None


def read_activation(config_home: Path, unit: str) -> Optional[Activation]:
    """The entry at the managed name for ``unit``, or ``None`` when absent."""
    path = dropin_path(config_home, unit)
    try:
        with _user_directory(path.parent) as directory:
            data = _read_user_file(directory, path.name, MAX_DROPIN_BYTES)
        return None if data is None else Activation(path, data.decode("utf-8"), True)
    except FileNotFoundError:
        return None
    except (OSError, MutterCaptureError, UnicodeDecodeError):
        return Activation(path, "", False)


def _read_override(path: Path) -> tuple:
    """Read the managed name without following a symlink.

    ``lstat`` alone would leave a window in which the entry is swapped for a
    symlink before the read; ``O_NOFOLLOW`` plus an ``fstat`` type check makes
    the answer describe the same object the bytes came from.
    """
    try:
        with _user_directory(path.parent) as directory:
            data = _read_user_file(directory, path.name, MAX_DROPIN_BYTES)
        return (data.decode("utf-8"), True) if data is not None else ("", False)
    except (OSError, MutterCaptureError, UnicodeDecodeError):
        return "", False


def scan_activation(config_home: Path) -> list:
    """Every entry at :data:`MANAGED_FILENAME` under ``org.gnome.Shell@*.d``.

    The scan is bounded and name-based, so a user's own drop-ins in the same
    directories are never touched.  Callers must check :attr:`Activation.managed`
    before overwriting or deleting: a file of that name may belong to somebody
    else, or be a symlink or directory that must be left alone.
    """
    base = Path(config_home) / "systemd" / "user"
    try:
        with _user_directory(base) as directory:
            names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except (OSError, MutterCaptureError):
        return [Activation(base / MANAGED_FILENAME, "", False)]
    names = [name for name in names if name.endswith(".service.d")
             and UNIT_PATTERN.fullmatch(name[:-2])]
    found = []
    for name in names[:MAX_DROPIN_DIRS]:
        entry = read_activation(config_home, name[:-2])
        if entry is not None:
            found.append(entry)
    return found


def activation_configured(config_home: Path, unit: str, root: str, mode: str,
                          state: str) -> bool:
    """The unit's own override is the exact schema-2 override for this runtime.

    A schema-1 override, a different root or mode, or a different guard state
    path is not configured; the effective ``ExecStart`` and hooks are checked
    separately from the live unit.
    """
    if not root or not mode or not state:
        return False
    entry = read_activation(config_home, unit)
    if entry is None or not entry.regular:
        return False
    override = entry.override
    return bool(
        override is not None and override.format == "guarded"
        and override.root == root and override.mode == mode
        and override.state == state
    )


def _write_atomic(path: Path, text: str, *, expected=_ANY_CONTENT) -> None:
    try:
        with _user_directory(path.parent, create=True) as directory:
            _replace_user_file(directory, path.name, text.encode("utf-8"),
                               expected=expected.encode("utf-8") if isinstance(expected, str) else expected)
    except OSError as exc:
        raise MutterCaptureError(f"cannot write {path}: {_error_name(exc)}") from exc


def _remove_tree(path: Path, *, expected=_ANY_CONTENT) -> None:
    try:
        with _user_directory(path.parent) as directory:
            data = _read_user_file(directory, path.name, MAX_DROPIN_BYTES)
            if expected is not _ANY_CONTENT and data != expected.encode("utf-8"):
                raise MutterCaptureError("the activation file changed during removal; it was left untouched")
            if data is None:
                return
            info = os.fstat(directory)
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MutterCaptureError(f"cannot remove {path}: {_error_name(exc)}") from exc
    try:
        with _user_directory(path.parent.parent) as parent:
            current = os.stat(path.parent.name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino):
                os.rmdir(path.parent.name, dir_fd=parent)
                os.fsync(parent)
    except (OSError, MutterCaptureError):
        pass


def _reload(runner: Runner, which: Which) -> None:
    """``systemctl --user daemon-reload``; the caller decides how to report it."""
    tool = which("systemctl")
    if not tool:
        raise MutterCaptureError("systemctl was not found, so nothing was changed")
    try:
        result = runner([tool, "--user", "daemon-reload"], 30)
    except Exception:  # noqa: BLE001
        raise MutterCaptureError("systemctl --user daemon-reload could not run") from None
    if _returncode(result) != 0:
        raise MutterCaptureError(
            f"systemctl --user daemon-reload failed with exit status {_returncode(result)}")


def _rollback(path: Path, previous: Optional[str], existed: bool,
              runner: Runner, which: Which, cause: MutterCaptureError, *,
              written=_ANY_CONTENT, guard_problem="") -> MutterCaptureError:
    """Restore the file, then reload again so systemd drops the applied change.

    A successful ``daemon-reload`` already replaced systemd's cached
    ``ExecStart``; rewriting the file alone would leave the rejected override
    in effect.  The second reload is part of the rollback, and a failure at any
    step is reported as an incomplete rollback rather than as a clean one.
    """
    guard_note = guard_problem or "startup guards remain blocked"
    try:
        if existed and previous is not None:
            _write_atomic(path, previous, expected=written)
        else:
            _remove_tree(path, expected=written)
    except MutterCaptureError as exc:
        return MutterCaptureError(f"{cause}; rollback incomplete: {exc}; {guard_note}")
    try:
        _reload(runner, which)
    except MutterCaptureError as exc:
        return MutterCaptureError(f"{cause}; rollback incomplete: {exc}; {guard_note}")
    if guard_problem:
        return MutterCaptureError(f"{cause}; activation file restored and service reloaded; "
                                 f"rollback incomplete: {guard_problem}")
    return MutterCaptureError(f"{cause}; activation configuration was rolled back; startup guards remain blocked")


def _restore_removed(pairs) -> Optional[str]:
    """Put back every already-removed override.  Returns a problem, if any."""
    problem = None
    for path, text in pairs:
        try:
            _write_atomic(path, text, expected=None)
        except MutterCaptureError as exc:
            problem = problem or str(exc)
    return problem


def _recover_removal(cause: MutterCaptureError, removed, runner: Runner,
                     which: Which, *, revoke_failed=None) -> MutterCaptureError:
    """Restore a failed removal, reload again, and describe what really happened.

    The reload is unconditional: the failed attempt may already have replaced
    systemd's cached configuration, so restoring the files without reloading
    would leave the half-applied state behind.
    """
    revoke_failed = revoke_failed or {}
    # Restoring an override whose guard could still be armed would re-enable
    # the activation the user was removing. Keep that file removed instead.
    kept_removed = [path for path, _ in removed if path in revoke_failed]
    problem = _restore_removed([(path, text) for path, text in removed if path not in revoke_failed])
    try:
        _reload(runner, which)
    except MutterCaptureError as exc:
        problem = problem or str(exc)
    if revoke_failed:
        note = "; ".join(f"startup guard revocation incomplete for {path}: {detail}"
                         for path, detail in revoke_failed.items())
        if kept_removed:
            note += "; " + "; ".join(f"activation file kept removed: {path}" for path in kept_removed)
        if problem:
            note += f"; rollback incomplete: {problem}"
        return MutterCaptureError(f"{cause}; {note}")
    if problem:
        return MutterCaptureError(f"{cause}; rollback incomplete: {problem}; startup guards remain blocked")
    return MutterCaptureError(f"{cause}; activation files were rolled back; "
                             "startup guards remain blocked until Set up exclusion succeeds")


def install_activation(*, config_home: Path, runtime: RuntimeInfo, shell: ShellInfo,
                        runner: Runner = None, which: Which = None,
                        proc_root: Optional[Path] = None) -> str:
    """Install or rearm the managed schema-2 override for one GNOME Shell unit.

    The transaction is ordered so a failure can never leave an armed guard
    without a matching override: validate the runtime, shell and existing file
    ownership; run the full isolated startup probe; render, reload and check the
    effective override; and only then arm the guard.  A failing probe changes
    nothing.  An already-configured but blocked activation is re-probed and
    rearmed rather than returned early, and a still-live guard is never rearmed.
    """
    runner = runner or _run
    which = which or shutil.which
    if not runtime.deployed:
        raise MutterCaptureError(
            runtime.reason or "the custom Mutter runtime is not deployed")
    if not shell.ok:
        raise MutterCaptureError(shell.reason or "the GNOME Shell unit is not identified")
    if not UNIT_PATTERN.fullmatch(shell.unit) or shell.unit != f"org.gnome.Shell@{shell.mode}.service":
        raise MutterCaptureError("the GNOME Shell unit and mode do not agree")
    if not which("systemctl"):
        raise MutterCaptureError("systemctl was not found, so nothing was changed")

    root = runtime.root
    mode = shell.mode
    state = guard_state_path(config_home, root, mode)
    content = render_dropin(root, mode, str(state))
    path = dropin_path(config_home, shell.unit)
    entry = read_activation(config_home, shell.unit)
    previous = None
    existed = False
    if entry is not None:
        if not entry.regular:
            raise MutterCaptureError(
                f"{path} is not a regular file SocketStream may read; it was left untouched")
        existed = True
        previous = entry.text
        if entry.override is None:
            raise MutterCaptureError(
                f"{path} already exists and was not written by SocketStream; "
                "it was left untouched")

    properties = _systemctl_show(runner, which, shell.unit)
    already = previous == content and _override_effective(properties, root, mode, str(state))
    guard = read_guard_state(state, root=root, mode=mode)
    if already and guard.armed:
        return f"activation is already installed and armed for {shell.unit}"
    if guard_matches_running(guard, shell, runtime, proc_root):
        if already and guard.phase == "ready" and read_live_state(shell, runtime, proc_root).active:
            return f"the guarded custom runtime is already active for {shell.unit}; capture pixels remain unverified"
        raise MutterCaptureError("a custom compositor attempt is still running; its startup guard cannot be rearmed")

    def same_service(properties):
        return bool(properties and properties.get("LoadState") == "loaded"
                    and properties.get("MainPID") == str(shell.pid)
                    and properties.get("Type") == "notify" and properties.get("NotifyAccess") == "main"
                    and exec_kind(properties.get("ExecStart", ""), mode) in ("ours", "stock"))

    if not same_service(properties):
        raise MutterCaptureError("the GNOME Shell service changed or does not provide main-process readiness")

    # Every install or rearm runs the isolated startup check first; a failure
    # here changes nothing on disk and arms nothing.
    ok, detail = run_startup_probe(root, mode, runner=runner)
    if not ok:
        raise MutterCaptureError(
            f"the isolated startup check did not pass, so nothing was changed: {detail}")

    fresh = read_activation(config_home, shell.unit)
    if ((entry is None) != (fresh is None)
            or (fresh is not None and (not fresh.managed or fresh.text != previous))
            or not same_service(_systemctl_show(runner, which, shell.unit))):
        raise MutterCaptureError("the activation or GNOME Shell service changed during the startup check; retry")

    # A state left armed by an earlier incomplete transaction cannot be used
    # while a replacement file is being applied. Retired hooks cannot rearm an
    # old runtime after this explicit change either.
    _revoke_guard(config_home, root, mode, str(state))
    retired = entry is not None and not entry.override.legacy and entry.override.state != str(state)
    if retired:
        try:
            _revoke_guard(config_home, entry.override.root, entry.override.mode, entry.override.state)
        except MutterCaptureError as exc:
            raise MutterCaptureError(
                f"{exc}; the new runtime's startup guard is blocked; the previous activation remains "
                "in place and its guard revocation is incomplete") from None

    wrote = False
    if not already:
        try:
            _write_atomic(path, content, expected=previous)
        except MutterCaptureError as exc:
            note = ("; the previous activation remains installed but its startup guard is now revoked"
                    if retired else "")
            raise MutterCaptureError(
                f"{exc}; the startup guard is blocked; activation installation did not complete{note}") from None
        wrote = True
        try:
            _reload(runner, which)
            properties = _systemctl_show(runner, which, shell.unit)
            if not _override_effective(properties, root, mode, str(state)):
                raise MutterCaptureError("systemd did not apply the activation override")
        except MutterCaptureError as exc:
            raise _rollback(path, previous, existed, runner, which, exc, written=content) from None

    try:
        arm_guard(root, mode, str(state), runner)
        properties = _systemctl_show(runner, which, shell.unit)
        if (not activation_configured(config_home, shell.unit, root, mode, str(state))
                or not _override_effective(properties, root, mode, str(state))):
            raise MutterCaptureError("activation changed while its startup guard was being armed")
    except MutterCaptureError as exc:
        problem = ""
        try:
            _revoke_guard(config_home, root, mode, str(state))
        except MutterCaptureError as revoke_error:
            problem = f"startup guard revocation incomplete: {revoke_error}"
        if wrote:
            raise _rollback(path, previous, existed, runner, which, exc,
                            written=content, guard_problem=problem) from None
        raise MutterCaptureError(f"{exc}; {problem or 'startup guard remains blocked'}") from None
    return (
        f"activation installed for {shell.unit}; log out and back in so GNOME Shell "
        "loads it"
    )


def remove_activation(*, config_home: Path, runner: Runner = None,
                      which: Which = None) -> str:
    """Remove every managed override, even if the session unit changed.

    Removal never needs the runtime or its packages to be present, which is
    what makes a stale activation recoverable.  A schema-2 guard is disarmed
    before its override disappears, so a late lifecycle hook cannot rearm a
    removed activation; when the runtime is already gone the state record is
    latched directly.
    """
    runner = runner or _run
    which = which or shutil.which
    found = scan_activation(config_home)
    if not found:
        return "no managed activation was installed"
    if not which("systemctl"):
        raise MutterCaptureError("systemctl was not found, so nothing was changed")

    revoke_failed = {}
    for entry in found:
        override = entry.override if entry.regular else None
        if override is not None and override.format == "guarded":
            try:
                _revoke_guard(config_home, override.root, override.mode, override.state)
            except MutterCaptureError as exc:
                revoke_failed[entry.path] = str(exc)

    removed, refused = [], []
    try:
        for entry in found:
            if not entry.managed:
                refused.append(entry.path)
                continue
            _remove_tree(entry.path, expected=entry.text)
            removed.append((entry.path, entry.text))
    except MutterCaptureError as exc:
        # A partial removal: restore what was already gone, then reload so
        # systemd does not keep the half-applied state either way.
        raise _recover_removal(exc, removed, runner, which, revoke_failed=revoke_failed) from None

    if not removed:
        raise MutterCaptureError(
            f"{refused[0]} was not written by SocketStream; it was left untouched")
    try:
        _reload(runner, which)
    except MutterCaptureError as exc:
        raise _recover_removal(exc, removed, runner, which, revoke_failed=revoke_failed) from None

    note = f"removed {len(removed)} managed activation file(s)"
    if refused:
        note += f"; {refused[0]} was not written by SocketStream and was left untouched"
    if revoke_failed:
        details = "; ".join(f"startup guard revocation incomplete for {path}: {detail}"
                            for path, detail in revoke_failed.items())
        raise MutterCaptureError(f"{note}; {details}")
    return note
