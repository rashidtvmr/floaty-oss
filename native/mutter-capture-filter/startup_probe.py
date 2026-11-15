#!/usr/bin/python3
"""Exercise full GNOME Shell JS/GI startup inside a disposable user service.

A pass requires the private executable's authenticated READY=1, four coherent
Mutter mappings and three seconds of continued liveness. Both D-Bus addresses
point at a private bus without service activation, and HOME/XDG are disposable.
This tests Shell startup, not the user's extensions, physical display or capture
pixels. The outer systemd service bounds memory/time and owns the whole tree.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import select
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

UNIT_PREFIX = "socket-stream-startup-check-"
MAX_LOG_BYTES = 256 * 1024
MAX_MAPS_BYTES = 8 * 1024 * 1024
STARTUP_SECONDS = 40
STABLE_SECONDS = 3
MODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")
LIBRARIES = tuple(f"lib/{name}.so.0" for name in (
    "libmutter-18", "libmutter-clutter-18", "libmutter-cogl-18", "libmutter-mtk-18",
))


class ProbeError(ValueError):
    """Fixed diagnostic; raw Shell logs are saved only with explicit --output."""


def _validator():
    file = Path(__file__).resolve().with_name("validate_runtime.py")
    spec = importlib.util.spec_from_file_location("socket_stream_probe_validator", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_identity(pid: int, *, proc: Path = Path("/proc")):
    """(start ticks, state); unlike a PID alone this survives reuse checks."""
    try:
        text = (proc / str(pid) / "stat").read_text()
        fields = text.rsplit(")", 1)[1].split()
        return int(fields[19]), fields[0]
    except (OSError, IndexError, ValueError):
        return None


def authenticated_sender(data, ancillary, flags, *, shell: Path, pgid: int,
                         uid: int, proc: Path = Path("/proc"), getpgid=os.getpgid):
    """dbus-daemon also sends READY; only our Shell's kernel credentials count."""
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or b"READY=1" not in data.splitlines():
        return None
    for level, kind, body in ancillary:
        if level != socket.SOL_SOCKET or kind != getattr(socket, "SCM_CREDENTIALS", -1) or len(body) != 12:
            continue
        pid, sender_uid, _ = struct.unpack("3i", body)
        try:
            identity = process_identity(pid, proc=proc)
            if (sender_uid == uid and getpgid(pid) == pgid and identity
                    and identity[1] != "Z" and os.path.samefile(proc / str(pid) / "exe", shell)):
                return pid, identity[0]
        except OSError:
            pass
    return None


def mapped_libraries(root: Path, pid: int, *, proc: Path = Path("/proc")) -> list[str]:
    """Every segment of every Mutter family mapping must be our exact inode."""
    expected = {}
    for name in LIBRARIES:
        file = root / name
        info = file.stat()
        expected[file.name] = (file, (os.major(info.st_dev), os.minor(info.st_dev)), info.st_ino)
    with (proc / str(pid) / "maps").open("rb") as stream:
        data = stream.read(MAX_MAPS_BYTES + 1)
    if len(data) > MAX_MAPS_BYTES:
        raise ProbeError("Shell mapping evidence exceeded its bound")
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
            raise ProbeError("Shell loaded an unexpected or deleted Mutter library")
        file, device, inode = item
        try:
            actual_device = tuple(int(part, 16) for part in fields[3].split(":"))
            matches = (Path(pathname) == file and actual_device == device and int(fields[4]) == inode)
        except ValueError:
            matches = False
        if not matches:
            raise ProbeError("Shell mixed stock and custom Mutter library mappings")
        found.add(basename)
    if found != set(expected):
        raise ProbeError("Shell did not load all four private Mutter libraries")
    return sorted(str(expected[name][0]) for name in found)


class Log:
    def __init__(self):
        self.tail = b""
        self.total = 0
        self.fatal = False

    def append(self, data):
        self.total += len(data)
        self.tail = (self.tail + data)[-MAX_LOG_BYTES:]
        self.fatal |= any(marker in self.tail for marker in (
            b"cannot register existing type", b"Unsupported type (null)",
            b"Execution of main.js threw exception",
        ))


def _drain(selector, log, timeout=0.1):
    notifications = []
    for key, _ in selector.select(timeout):
        if key.data == "notify":
            try:
                notifications.append(key.fileobj.recvmsg(8192, socket.CMSG_SPACE(12)))
            except BlockingIOError:
                pass
        else:
            try:
                data = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if data:
                log.append(data)
            else:
                selector.unregister(key.fileobj)
    return notifications


def worker_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            relative = Path(line[3:].lstrip("/"))
            if (relative.name.startswith(UNIT_PREFIX) and relative.name.endswith(".service")
                    and ".." not in relative.parts):
                return Path("/sys/fs/cgroup") / relative
    raise ProbeError("startup worker must run in its disposable systemd service")


def _members(cgroup: Path) -> list[int]:
    members = []
    for value in (cgroup / "cgroup.procs").read_text().splitlines():
        pid = int(value)
        identity = process_identity(pid)
        if pid != os.getpid() and identity and identity[1] != "Z":
            members.append(pid)
    return members


def _cleanup(process, shell_fd, cgroup, selector, log) -> bool:
    # Stop Shell before Xwayland, so our shutdown does not resemble a startup
    # failure. Then reap all helpers by pidfd inside ONLY our dedicated cgroup.
    if shell_fd is not None:
        try:
            signal.pidfd_send_signal(shell_fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while process.poll() is None and time.monotonic() < deadline:
        _drain(selector, log)
    for sig, seconds in ((signal.SIGTERM, 1), (signal.SIGKILL, 2)):
        for pid in _members(cgroup):
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                # Recheck membership after opening to avoid a recycled PID.
                if pid in _members(cgroup):
                    signal.pidfd_send_signal(descriptor, sig)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)
        deadline = time.monotonic() + seconds
        while _members(cgroup) and time.monotonic() < deadline:
            _drain(selector, log)
            process.poll()
        if not _members(cgroup):
            break
    process.poll()
    return not _members(cgroup) and process.returncode is not None


def _environment(sandbox: Path, root: Path) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(sandbox / "home"),
        "XDG_RUNTIME_DIR": str(sandbox / "runtime"),
        "XDG_CONFIG_HOME": str(sandbox / "config"), "XDG_CACHE_HOME": str(sandbox / "cache"),
        "XDG_DATA_HOME": str(sandbox / "data"), "XDG_STATE_HOME": str(sandbox / "state"),
        "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        "XDG_DATA_DIRS": "/usr/share/ubuntu:/usr/local/share:/usr/share",
        "GSETTINGS_BACKEND": "memory", "GIO_USE_VFS": "local", "GVFS_DISABLE_FUSE": "1",
        "GTK_USE_PORTAL": "0", "LIBGL_ALWAYS_SOFTWARE": "1", "LC_ALL": "C.UTF-8",
        "SOCKET_STREAM_CAPTURE_EXCLUDE": "1",
    }
    # Match the launcher's loader path construction after removing old runtimes.
    inherited = _validator().clean_environment(root)
    for key in ("LD_LIBRARY_PATH", "GI_TYPELIB_PATH"):
        if inherited.get(key):
            env[key] = inherited[key]
    return _validator().loader_environment(root, env)


def _runtime_directory() -> Path:
    runtime = Path("/run/user") / str(os.getuid())
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProbeError("the user runtime directory is not private")
    return runtime


def run_headless(root: Path, mode: str, *, shell: Path | None = None,
                 output: Path | None = None, startup_seconds=STARTUP_SECONDS,
                 stable_seconds=STABLE_SECONDS) -> dict:
    """Worker implementation; shell override is only an in-process regression seam."""
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ProbeError("run the startup check as the desktop user on Linux")
    cgroup = worker_cgroup()
    runtime_base = _runtime_directory()
    if output is not None:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    shell = root / "gnome-shell" if shell is None else shell
    result = {"ok": False, "ready": False, "runtime": str(root), "mode": mode}
    log = Log()
    temporary = tempfile.TemporaryDirectory(prefix="ss-shell-", dir=runtime_base)
    try:
        sandbox = Path(temporary.name)
        for name in ("home", "runtime", "config", "cache", "data", "state"):
            (sandbox / name).mkdir(mode=0o700)
        (sandbox / "home/Desktop").mkdir(mode=0o700)
        config = sandbox / "bus.conf"
        config.write_text(
            '<busconfig><type>session</type><listen>unix:tmpdir=' + str(sandbox)
            + '</listen><auth>EXTERNAL</auth><policy context="default">'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow own="*"/></policy></busconfig>')
        notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        notify.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        address = "socket-stream-startup-" + secrets.token_hex(12)
        notify.bind("\0" + address)
        notify.setblocking(False)
        command = [
            "/usr/bin/dbus-run-session", "--config-file=" + str(config), "--",
            "/bin/sh", "-c", 'exec /usr/bin/env DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" "$@"',
            "socket-stream-probe", "/usr/bin/env", "NOTIFY_SOCKET=@" + address, str(shell),
            "--headless", "--wayland", "--virtual-monitor=1280x720", "--mode=" + mode,
        ]
        started = time.monotonic()
        ready_at, shell_fd = None, None
        process = None
        startup_log = b""
        with selectors.DefaultSelector() as selector:
            try:
                process = subprocess.Popen(command, env=_environment(sandbox, root), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ, "log")
                selector.register(notify, selectors.EVENT_READ, "notify")
                while process.poll() is None and time.monotonic() - started < startup_seconds:
                    for data, ancillary, flags, _ in _drain(selector, log):
                        sender = authenticated_sender(data, ancillary, flags, shell=shell,
                                                      pgid=process.pid, uid=os.getuid())
                        if sender is not None and ready_at is None:
                            pid, ticks = sender
                            shell_fd = os.pidfd_open(pid)
                            result.update(ready=True, ready_pid=pid, starttime=ticks,
                                          mutter_mappings=mapped_libraries(root, pid))
                            ready_at = time.monotonic()
                    if log.fatal:
                        raise ProbeError("full Shell startup reported a fatal JS or duplicate-GType error")
                    if ready_at is not None:
                        if select.select([shell_fd], [], [], 0)[0]:
                            raise ProbeError("private Shell exited after announcing readiness")
                        if time.monotonic() - ready_at >= stable_seconds:
                            pid = result["ready_pid"]
                            if process_identity(pid) != (result["starttime"], "S"):
                                current = process_identity(pid)
                                if not current or current[0] != result["starttime"] or current[1] == "Z":
                                    raise ProbeError("private Shell process identity changed")
                            result["mutter_mappings"] = mapped_libraries(root, pid)
                            result["stable_seconds"] = round(time.monotonic() - ready_at, 3)
                            result["ok"] = True
                            break
                if not result["ok"]:
                    raise ProbeError("private Shell exited or timed out before stable readiness")
            except (ProbeError, OSError) as exc:
                result["reason"] = str(exc) if isinstance(exc, ProbeError) else "startup evidence could not be read"
            finally:
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if process is not None:
                    try:
                        result["cleanup_complete"] = _cleanup(process, shell_fd, cgroup, selector, log)
                    except OSError:
                        result["cleanup_complete"] = False
                    result["shutdown_returncode"] = process.returncode
                    # Processes may have exited with a final fatal line still
                    # buffered in stdout. Drain the bounded pipe before closing.
                    for _ in range(4):
                        before = log.total
                        _drain(selector, log, timeout=0)
                        if log.total == before:
                            break
                    process.stdout.close()
                else:
                    result["cleanup_complete"] = True
                if shell_fd is not None:
                    os.close(shell_fd)
                notify.close()
                if not result["cleanup_complete"]:
                    result["ok"] = False
                    result["reason"] = "startup worker could not confirm complete process cleanup"
                elif result["ok"] and result.get("shutdown_returncode") != 0:
                    result["ok"] = False
                    result["reason"] = "private Shell did not shut down cleanly after the startup check"
                elif result["ok"] and log.fatal:
                    result["ok"] = False
                    result["reason"] = "full Shell startup reported a fatal JS or duplicate-GType error"
                result["log_bytes"] = log.total
                startup_log = log.tail
        if output is not None:
            (output / "shell.log").write_bytes(startup_log)
    finally:
        try:
            temporary.cleanup()
        except OSError:
            result.update(ok=False, cleanup_complete=False,
                          reason="startup worker could not remove its disposable home/runtime tree")
    if output is not None:
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run_isolated(root: Path, mode: str, output: Path | None = None, *, run=subprocess.run) -> dict:
    """The outer controller waits for systemd's cgroup cleanup before reporting."""
    unit = UNIT_PREFIX + secrets.token_hex(12) + ".service"
    command = [
        "/usr/bin/systemd-run", "--user", "--wait", "--pipe", "--quiet", "--collect",
        "--service-type=exec", "--unit=" + unit, "--property=KillMode=control-group",
        "--property=MemoryMax=1G", "--property=RuntimeMaxSec=50s", "--property=TimeoutStopSec=5s",
        "--property=TasksMax=512",
        "/usr/bin/python3", "-I", str(root / "startup_probe.py"), "--worker",
        "--runtime", str(root), "--mode", mode,
    ]
    if output is not None:
        command.extend(("--output", str(output)))
    try:
        completed = run(command, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        # Only the fresh, unpredictable unit created by this invocation is stopped.
        try:
            run(["/usr/bin/systemctl", "--user", "stop", unit], capture_output=True,
                text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        raise ProbeError("isolated startup service timed out; activation was not armed") from exc
    if len(completed.stdout) > 65536:
        raise ProbeError("startup service returned oversized evidence")
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise ProbeError("isolated startup service was unavailable or returned no evidence") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ProbeError("startup service returned invalid evidence")
    if result["ok"] and not (
            result.get("runtime") == str(root) and result.get("mode") == mode
            and result.get("ready") is True and result.get("cleanup_complete") is True
            and type(result.get("shutdown_returncode")) is int and result["shutdown_returncode"] == 0
            and type(result.get("ready_pid")) is int and result["ready_pid"] > 0
            and type(result.get("starttime")) is int and result["starttime"] > 0
            and type(result.get("stable_seconds")) in (int, float)
            and STABLE_SECONDS <= result["stable_seconds"] <= STARTUP_SECONDS
            and result.get("mutter_mappings") == sorted(str(root / name) for name in LIBRARIES)):
        raise ProbeError("startup service claimed success without complete matching evidence")
    if completed.returncode != 0:
        result["ok"] = False
        result.setdefault("reason", "isolated startup service failed")
    result["unit"] = unit
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    result = {"ok": False}
    try:
        if sys.platform != "linux" or os.geteuid() == 0 or not MODE.fullmatch(args.mode):
            raise ProbeError("run the startup check as the desktop user on supported Linux")
        validator = _validator()
        root, _ = validator.validate_runtime(args.runtime / "runtime.json")
        if args.worker:
            result = run_headless(root, args.mode, output=args.output)
        else:
            result = run_isolated(root, args.mode, args.output)
    except ProbeError as exc:
        result["reason"] = str(exc)
    except Exception:
        result["reason"] = "startup runtime validation or isolation could not complete"
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
