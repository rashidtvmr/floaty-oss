#!/usr/bin/python3
"""Real-Linux integration QA for ``native/mutter-capture-filter/startup_guard.py``.

This harness drives the *production* guard through its real CLI and in-process
API against a real ``/proc`` and real disposable ``systemd --user`` services of
Type=notify. It is deliberately not another mock of the guard internals: the
only synthetic parts are the fixture runtime tree and its two executable
fixtures.

Usage (run as the desktop user, never as root)::

    /usr/bin/python3 run_startup_guard_qa.py \\
        --runtime /opt/socket_stream-mutter/<published-root> \\
        --output /absolute/new/evidence/dir

``--runtime`` is the published runtime directory that ships the production
``startup_guard.py``; it is only used as the source of that module. Guard
actions run against a private fixture runtime root created under a 0700 QA
scratch directory in ``/run/user/UID``, which the guard accepts because it is a
canonical, same-UID, trusted tree.

Covered cases (each observed from real systemd results, real ``/proc`` and the
real guard record):

1. clean login cycle: arm -> consume -> ready -> main exit 0 -> re-armed;
2. exit 1 before READY -> blocked (pending-failed);
3. new invocation after that failure, without re-arming -> the launcher must
   fall back to the stock fixture; the stock READY hook cannot promote and the
   late ExecStopPost cannot re-arm the stale blocked record;
4. post-readiness SIGSEGV (or SIGKILL via ``--crash-signal kill``) -> blocked;
5. new invocation after the crash -> stock fallback, still blocked;
6. READY then exit 1 although ``SuccessExitStatus=1`` makes the service result
   successful -> blocked (the critical Finish tuple trap);
7. new invocation after that exit -> stock fallback, still blocked;
8. disarm while the record is ready and the service is still live -> blocked,
   and the late clean ExecStopPost cannot re-arm it.

The fixture main process is a small C program compiled with the system
``/usr/bin/cc``. It is named ``gnome-shell`` inside the fixture runtime and it
maps, read-only, four synthetic regular files at the exact private paths
``ROOT/lib/libmutter-18.so.0``, ``libmutter-clutter-18.so.0``,
``libmutter-cogl-18.so.0`` and ``libmutter-mtk-18.so.0`` before announcing
READY, so ``/proc/PID/maps`` records the expected paths and inodes. A separate
copy (different inode) is the stock fixture.

Explicit limits, also recorded in ``summary.json``:

* the fixture is **not** Mutter and never loads, links or runs the synthetic
  libraries; native manifest/schema validation, loader resolution, ABI and
  compositor behaviour are out of scope (the public full-Shell probe covers
  those);
* no compositor is launched, no application is activated, no GNOME unit, user
  profile or desktop bus is inspected or changed;
* the four public guard APIs/CLI semantics under test are the production ones;
  double-consume concurrency is intentionally left to
  ``tests/test_mutter_guard.py``;
* service ``Result``/``ExecMainCode``/``ExecMainStatus`` are recorded from the
  exact ``SERVICE_RESULT``/``EXIT_CODE``/``EXIT_STATUS`` environment systemd
  supplies to ExecStopPost, never inferred from the harness return code.

Requires Linux, a working ``systemd --user`` manager, ``/usr/bin/cc`` and
``/usr/bin/python3``. It cannot run on macOS; only syntax checks are possible
there.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

PREFIX = "socket-stream-guard-qa-"
MODE = "ubuntu"
PYTHON = "/usr/bin/python3"
CC = "/usr/bin/cc"
SYSTEMD_RUN = shutil.which("systemd-run") or "/usr/bin/systemd-run"
SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"

START_TIMEOUT = 8
STOP_TIMEOUT = 3
RUNTIME_MAX = 15
WAIT_SLACK = 6
ACCEPT_TIMEOUT = 6
MAX_STATE_BYTES = 8192
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 4000
MAX_JOURNAL_LINES = 400

FIXTURE_ROOT_NAME = "50.99-0ubuntu2.5.socket-stream.0011223344556677"
LIBRARIES = (
    "libmutter-18.so.0",
    "libmutter-clutter-18.so.0",
    "libmutter-cogl-18.so.0",
    "libmutter-mtk-18.so.0",
)
LIBRARY_RELATIVE = tuple("lib/" + name for name in LIBRARIES)

UNIT_PATTERN = re.compile(r"socket-stream-guard-qa-[a-z0-9][a-z0-9-]{0,80}\Z")
SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.+\-]{1,160}\Z")
IDENTITY_FALLBACK = ("invocation_id", "boot_id", "pid", "starttime")
PHASES = ("armed", "pending", "ready", "blocked")

HARNESS_LIMITS = [
    "The fixture runtime is synthetic: a C executable named gnome-shell maps four "
    "synthetic one-page files at ROOT/lib/libmutter-*.so.0. It does not load or "
    "execute Mutter, so native runtime validation, schema/manifest checks, loader "
    "resolution, ABI compatibility and compositor capture behaviour are NOT tested. "
    "A guard mapping check therefore sees correct synthetic paths and inodes only; "
    "it does not establish that a real published runtime is natively valid.",
    "The --runtime argument only supplies the production startup_guard.py module; "
    "guard actions run against the private 0700 fixture runtime root under "
    "/run/user/UID created by this harness.",
    "No compositor is started, no app is activated, and no GNOME unit, user profile, "
    "live desktop bus or session configuration is inspected or changed.",
    "The arm/consume/ready/finish/disarm/status guard actions are exercised against "
    "real /proc and real systemd ExecStartPost/ExecStopPost; double-consume "
    "concurrency is covered by "
    "tests/test_mutter_guard.py instead. Timeout, OOM-kill, missing-finish and "
    "power-loss cases are not simulated; the restart cases cover the stale blocked "
    "record path, and no case re-arms before restarting.",
    "Service Result/ExecMainCode/ExecMainStatus values are captured from systemd's "
    "SERVICE_RESULT/EXIT_CODE/EXIT_STATUS ExecStopPost environment, not inferred.",
    "Linux-only: the harness cannot execute on macOS; only syntax compilation is "
    "possible there.",
]

FIXTURE_C = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#define FIXTURE_LIBRARIES 4

static const char *const library_names[FIXTURE_LIBRARIES] = {
    "libmutter-18.so.0",
    "libmutter-clutter-18.so.0",
    "libmutter-cogl-18.so.0",
    "libmutter-mtk-18.so.0",
};
static void *library_mappings[FIXTURE_LIBRARIES];

static int address_for(const char *path, struct sockaddr_un *address, socklen_t *length) {
    size_t size = strlen(path);
    if (size == 0 || size >= sizeof(address->sun_path)) {
        return -1;
    }
    memset(address, 0, sizeof(*address));
    address->sun_family = AF_UNIX;
    if (path[0] == '@') {
        address->sun_path[0] = '\0';
        memcpy(address->sun_path + 1, path + 1, size - 1);
        *length = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + (size - 1));
    } else {
        memcpy(address->sun_path, path, size + 1);
        *length = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + size + 1);
    }
    return 0;
}

static int notify_ready(void) {
    const char *path = getenv("NOTIFY_SOCKET");
    if (path == NULL || *path == '\0') {
        return 0;
    }
    struct sockaddr_un address;
    socklen_t length;
    if (address_for(path, &address, &length) != 0) {
        return -1;
    }
    int descriptor = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (descriptor < 0) {
        return -1;
    }
    const char message[] = "READY=1\n";
    ssize_t sent = sendto(descriptor, message, sizeof(message) - 1, 0,
                          (struct sockaddr *)&address, length);
    int result = sent == (ssize_t)(sizeof(message) - 1) ? 0 : -1;
    close(descriptor);
    return result;
}

/* Private, read-only mappings at the exact runtime paths the guard inspects. */
static int map_libraries(void) {
    const char *root = getenv("SOCKET_STREAM_QA_LIBRARY_ROOT");
    if (root == NULL || *root == '\0') {
        return 0;
    }
    for (int index = 0; index < FIXTURE_LIBRARIES; index++) {
        char path[4096];
        int written = snprintf(path, sizeof(path), "%s/lib/%s", root, library_names[index]);
        if (written <= 0 || (size_t)written >= sizeof(path)) {
            return -1;
        }
        int descriptor = open(path, O_RDONLY | O_CLOEXEC);
        if (descriptor < 0) {
            fprintf(stderr, "socket-stream qa fixture: cannot open %s: %s\n", path, strerror(errno));
            return -1;
        }
        struct stat info;
        if (fstat(descriptor, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size < 1) {
            fprintf(stderr, "socket-stream qa fixture: invalid library inode %s\n", path);
            close(descriptor);
            return -1;
        }
        void *mapping = mmap(NULL, (size_t)info.st_size, PROT_READ, MAP_PRIVATE, descriptor, 0);
        close(descriptor);
        if (mapping == MAP_FAILED) {
            fprintf(stderr, "socket-stream qa fixture: mmap failed for %s\n", path);
            return -1;
        }
        library_mappings[index] = mapping;
    }
    for (int index = 0; index < FIXTURE_LIBRARIES; index++) {
        volatile const unsigned char *page = library_mappings[index];
        (void)page[0];
    }
    return 0;
}

static int connect_control(void) {
    const char *path = getenv("SOCKET_STREAM_QA_CONTROL");
    if (path == NULL || *path == '\0') {
        return -1;
    }
    struct sockaddr_un address;
    socklen_t length;
    if (address_for(path, &address, &length) != 0) {
        return -1;
    }
    int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0) {
        return -1;
    }
    if (connect(descriptor, (struct sockaddr *)&address, length) != 0) {
        close(descriptor);
        return -1;
    }
    return descriptor;
}

static int read_command(int descriptor, char *buffer, size_t capacity) {
    size_t used = 0;
    while (used + 1 < capacity) {
        char byte = '\0';
        ssize_t got = read(descriptor, &byte, 1);
        if (got <= 0) {
            break;
        }
        if (byte == '\n') {
            break;
        }
        buffer[used++] = byte;
    }
    buffer[used] = '\0';
    return 0;
}

int main(void) {
    if (getenv("SOCKET_STREAM_QA_EXIT_BEFORE_READY") != NULL) {
        fprintf(stderr, "socket-stream qa fixture: exiting before READY\n");
        return 1;
    }
    if (map_libraries() != 0) {
        return 72;
    }
    if (notify_ready() != 0) {
        fprintf(stderr, "socket-stream qa fixture: READY notification failed\n");
        return 70;
    }
    int control = connect_control();
    if (control < 0) {
        fprintf(stderr, "socket-stream qa fixture: control channel connect failed\n");
        return 71;
    }
    char command[64];
    read_command(control, command, sizeof(command));
    close(control);
    if (command[0] == '\0') {
        fprintf(stderr, "socket-stream qa fixture: control channel closed without a command\n");
        return 73;
    }
    if (strcmp(command, "exit1") == 0) {
        return 1;
    }
    if (strcmp(command, "segv") == 0) {
        raise(SIGSEGV);
        return 139;
    }
    if (strcmp(command, "kill") == 0) {
        kill(getpid(), SIGKILL);
        return 137;
    }
    if (strcmp(command, "abort") == 0) {
        raise(SIGABRT);
        return 134;
    }
    return 0;
}
"""

LAUNCHER = r'''
#!/usr/bin/python3
"""Same-PID fixture launcher: production guard consume(), then exec a fixture.

The production guard is loaded by path (as production does under -I) and
registered in sys.modules. On a successful claim the compositor fixture is
exec'd with the capture opt-in exported only into that process. On any refusal
the separate stock fixture is exec'd with the opt-in removed, exactly as the
login fallback must behave.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys


def load_guard(path):
    spec = importlib.util.spec_from_file_location("startup_guard", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["startup_guard"] = module
    spec.loader.exec_module(module)
    return module


def write_branch(directory, record):
    name = str(record.get("invocation_id") or os.environ.get("INVOCATION_ID") or "unknown")
    name = "".join(character for character in name
                  if character.isalnum() or character in "-_")[:64] or "unknown"
    path = Path(directory) / (name + ".json")
    temporary = Path(str(path) + ".tmp")
    try:
        with temporary.open("w", encoding="ascii") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError:
        pass


def exec_stock(args, environment, branch):
    branch["branch"] = "stock"
    write_branch(args.branch_dir, branch)
    environment.pop("SOCKET_STREAM_CAPTURE_EXCLUDE", None)
    environment.pop("SOCKET_STREAM_QA_LIBRARY_ROOT", None)
    environment.pop("SOCKET_STREAM_QA_EXIT_BEFORE_READY", None)
    environment["SOCKET_STREAM_QA_CONTROL"] = args.control
    os.execve(args.stock, [args.stock, "stock"], environment)


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--guard", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--custom", required=True)
    parser.add_argument("--stock", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--branch-dir", required=True)
    parser.add_argument("--library-root", required=True)
    parser.add_argument("--exit-before-ready", action="store_true")
    args = parser.parse_args(argv[1:])

    branch = {"pid": os.getpid(), "invocation_id": os.environ.get("INVOCATION_ID", "")}
    try:
        guard = load_guard(args.guard)
    except Exception as error:
        branch.update(branch="error", detail="guard-import-failed",
                      error=type(error).__name__)
        write_branch(args.branch_dir, branch)
        return 70

    environment = dict(os.environ)
    try:
        record = guard.consume(Path(args.state), Path(args.runtime), args.mode)
    except guard.GuardError as error:
        branch.update(detail="guard-refused", reason=error.code)
        exec_stock(args, environment, branch)
    except Exception as error:
        branch.update(detail="guard-exception", error=type(error).__name__)
        exec_stock(args, environment, branch)

    branch.update(branch="custom", detail="guard-claimed", phase=record["phase"],
                  claim_pid=record.get("pid"),
                  claim_starttime=record.get("starttime"))
    write_branch(args.branch_dir, branch)
    environment["SOCKET_STREAM_CAPTURE_EXCLUDE"] = "1"
    environment["SOCKET_STREAM_QA_CONTROL"] = args.control
    environment["SOCKET_STREAM_QA_LIBRARY_ROOT"] = args.library_root
    if args.exit_before_ready:
        environment["SOCKET_STREAM_QA_EXIT_BEFORE_READY"] = "1"
    os.execve(args.custom, [args.custom, "custom"], environment)
    return 73


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''

POST_HOOK = r'''
#!/usr/bin/python3
"""ExecStartPost evidence hook (never the production guard itself).

Runs after the production ready hook, snapshots the raw guard record plus the
main process identity from the real /proc, and sends one small datagram so the
controller can prove ExecStartPost finished before inducing any exit.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys

LIBRARIES = (
    "libmutter-18.so.0",
    "libmutter-clutter-18.so.0",
    "libmutter-cogl-18.so.0",
    "libmutter-mtk-18.so.0",
)
MAX_MAPS = 8 * 1024 * 1024


def load_guard(path):
    spec = importlib.util.spec_from_file_location("startup_guard", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["startup_guard"] = module
    spec.loader.exec_module(module)
    return module


def read_record(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {"state_error": "unreadable"}
    if not data or len(data) > 8192:
        return {"state_error": "invalid-size"}
    try:
        record = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return {"state_error": "invalid-json"}
    return record if isinstance(record, dict) else {"state_error": "invalid-record"}


def identity(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return [info.st_dev, info.st_ino]


def mutter_mappings(pid, library_root):
    root = Path(library_root)
    expected = {}
    for name in LIBRARIES:
        try:
            info = os.stat(root / "lib" / name)
        except OSError:
            return {"error": "fixture-library-missing", "entries": []}
        expected[name] = (root / "lib" / name, info.st_dev, info.st_ino)
    try:
        with (Path("/proc") / str(pid) / "maps").open("rb") as stream:
            data = stream.read(MAX_MAPS + 1)
    except OSError:
        return {"error": "maps-unreadable", "entries": []}
    if len(data) > MAX_MAPS:
        return {"error": "maps-oversized", "entries": []}
    entries = []
    for line in data.decode("utf-8", "replace").splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        pathname = fields[5]
        basename = Path(pathname.removesuffix(" (deleted)")).name
        if not basename.startswith("libmutter"):
            continue
        entry = {"name": basename, "path": pathname,
                 "deleted": pathname.endswith(" (deleted)")}
        try:
            device = [int(part, 16) for part in fields[3].split(":")]
            inode = int(fields[4])
        except ValueError:
            entry["error"] = "unreadable-fields"
            entries.append(entry)
            continue
        expected_item = expected.get(basename)
        matches = bool(expected_item) and (
            Path(pathname) == expected_item[0]
            and inode == expected_item[2]
            and device == [os.major(expected_item[1]), os.minor(expected_item[1])]
        )
        entry.update(inode=inode, device=device, matches_expected=matches)
        entries.append(entry)
    names = {entry.get("name") for entry in entries}
    complete = (names == set(LIBRARIES)
                and all(entry.get("matches_expected") is True for entry in entries))
    return {"entries": entries, "complete": complete,
            "expected_paths": [str(expected[name][0]) for name in LIBRARIES]}


def write_snapshot(path, payload):
    path = Path(path)
    temporary = Path(str(path) + ".tmp")
    try:
        with temporary.open("w", encoding="ascii") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError:
        pass


def send_event(path, payload):
    try:
        data = json.dumps({"kind": "post", "token": payload["token"],
                           "data": payload}, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return
    if len(data) > 60000:
        return
    try:
        descriptor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            descriptor.sendto(data, path)
        finally:
            descriptor.close()
    except OSError:
        pass


def main(argv):
    if len(argv) != 9:
        return 64
    (guard_path, state_path, socket_path, snapshot_path,
     token, library_root, fixture_shell, stock_shell) = argv[1:]
    snapshot = {"kind": "post", "token": token,
                "invocation_id": os.environ.get("INVOCATION_ID"),
                "main_pid": os.environ.get("MAINPID"),
                "helper_optin": "SOCKET_STREAM_CAPTURE_EXCLUDE" in os.environ,
                "state": read_record(state_path)}
    try:
        pid = int(snapshot["main_pid"])
    except (TypeError, ValueError):
        pid = None
    snapshot["main_pid"] = pid
    if pid is not None:
        try:
            guard = load_guard(guard_path)
            proc = guard.Proc()
            snapshot["process_owner"] = proc.owner(pid)
            snapshot["process_starttime"] = proc.starttime(pid)
            snapshot["process_executable"] = proc.executable(pid)
            try:
                snapshot["main_environment"] = proc.environment(pid)
            except guard.GuardError as error:
                snapshot["main_environment"] = {}
                snapshot["main_environment_error"] = error.code
            except OSError:
                snapshot["main_environment"] = {}
                snapshot["main_environment_error"] = "environment-unreadable"
        except Exception as error:
            snapshot["process_error"] = type(error).__name__
        snapshot["fixture_executable"] = identity(fixture_shell)
        snapshot["stock_executable"] = identity(stock_shell)
        executable = snapshot.get("process_executable")
        snapshot["main_is_fixture"] = executable is not None and executable == tuple(
            snapshot["fixture_executable"]) if snapshot["fixture_executable"] else False
        snapshot["main_is_stock"] = executable is not None and executable == tuple(
            snapshot["stock_executable"]) if snapshot["stock_executable"] else False
        snapshot["mutter_mappings"] = mutter_mappings(pid, library_root)
    else:
        snapshot["process_error"] = "no-main-pid"
        snapshot["mutter_mappings"] = {"error": "no-main-pid", "entries": []}
    write_snapshot(snapshot_path, snapshot)
    send_event(socket_path, snapshot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception:
        raise SystemExit(1)
'''

STOP_HOOK = r'''
#!/usr/bin/python3
"""ExecStopPost evidence hook: the exact systemd Finish tuple and raw record."""
import json
import os
from pathlib import Path
import socket
import sys


def read_record(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {"state_error": "unreadable"}
    if not data or len(data) > 8192:
        return {"state_error": "invalid-size"}
    try:
        record = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return {"state_error": "invalid-json"}
    return record if isinstance(record, dict) else {"state_error": "invalid-record"}


def main(argv):
    if len(argv) != 5:
        return 64
    state_path, socket_path, snapshot_path, token = argv[1:]
    snapshot = {
        "kind": "stop",
        "token": token,
        "invocation_id": os.environ.get("INVOCATION_ID"),
        "main_pid": os.environ.get("MAINPID"),
        "service_result": os.environ.get("SERVICE_RESULT"),
        "exit_code": os.environ.get("EXIT_CODE"),
        "exit_status": os.environ.get("EXIT_STATUS"),
        "helper_optin": "SOCKET_STREAM_CAPTURE_EXCLUDE" in os.environ,
        "state": read_record(state_path),
    }
    path = Path(snapshot_path)
    temporary = Path(str(path) + ".tmp")
    try:
        with temporary.open("w", encoding="ascii") as stream:
            json.dump(snapshot, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError:
        pass
    try:
        data = json.dumps({"kind": "stop", "token": token, "data": snapshot},
                          sort_keys=True).encode("utf-8")
        if len(data) <= 60000:
            descriptor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                descriptor.sendto(data, socket_path)
            finally:
                descriptor.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception:
        raise SystemExit(1)
'''

MARKER_HOOK = r'''
import sys
from pathlib import Path
try:
    with Path(sys.argv[1]).open("a", encoding="ascii") as stream:
        stream.write("ok\n")
except OSError:
    raise SystemExit(1)
'''


class SetupError(RuntimeError):
    """The harness cannot run here; nothing about guard semantics is concluded."""


class HarnessError(RuntimeError):
    """The fixture or controller machinery failed, not the guard expectation."""


class CaseAborted(RuntimeError):
    """A case expectation failed early enough that no exit should be induced."""


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def truncate(text, limit=MAX_OUTPUT_CHARS):
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(json_safe(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def atomic_json(path, payload):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + os.urandom(8).hex() + ".tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(json_safe(payload), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_summary(record):
    if not isinstance(record, dict) or not record:
        return {"phase": "missing"}
    summary = {}
    for key in ("phase", "reason", "pid", "starttime",
                "invocation_id", "boot_id", "identity"):
        if key in record:
            summary[key] = record[key]
    if "phase" not in summary:
        summary["phase"] = "invalid"
    return summary


def variant(signal_name):
    return {"segv": (11, "SEGV"), "kill": (9, "KILL")}[signal_name]


def build_specs(crash_signal):
    crash_number, crash_name = variant(crash_signal)
    crash_status = [str(crash_number), crash_name, crash_name.lower()]
    crash = "post-ready-" + crash_signal
    return [
        {"name": "clean-ready-exit0", "arm": True, "pre_ready": False,
         "command": "exit0", "branch": "custom", "ready_phase": "ready",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["0"]},
         "rc": "zero", "after_phase": "armed", "after_reason": "clean-stop",
         "after_identity": False, "disarm": False, "restart_of": None},
        {"name": "pre-ready-exit1", "arm": True, "pre_ready": True,
         "command": None, "branch": "custom", "ready_phase": None,
         "finish": {"exit_code": ["exited"], "exit_status": ["1"]},
         "rc": "nonzero", "after_phase": "blocked", "after_reason": "pending-failed",
         "after_identity": True, "disarm": False, "restart_of": None},
        {"name": "pre-ready-exit1-restart", "arm": False, "pre_ready": False,
         "command": "exit0", "branch": "stock", "ready_phase": "blocked",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["0"]},
         "rc": "zero", "after_phase": "blocked", "after_reason": "pending-failed",
         "after_identity": True, "disarm": False,
         "restart_of": "pre-ready-exit1"},
        {"name": crash, "arm": True, "pre_ready": False,
         "command": crash_signal, "branch": "custom", "ready_phase": "ready",
         "finish": {"service_result": ["signal", "core-dump"],
                    "exit_status": crash_status},
         "rc": "nonzero", "after_phase": "blocked", "after_reason": "unclean-stop",
         "after_identity": True, "disarm": False, "restart_of": None},
        {"name": crash + "-restart", "arm": False, "pre_ready": False,
         "command": "exit0", "branch": "stock", "ready_phase": "blocked",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["0"]},
         "rc": "zero", "after_phase": "blocked", "after_reason": "unclean-stop",
         "after_identity": True, "disarm": False, "restart_of": crash},
        {"name": "ready-exit1", "arm": True, "pre_ready": False,
         "command": "exit1", "branch": "custom", "ready_phase": "ready",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["1"]},
         "rc": "any", "after_phase": "blocked", "after_reason": "unclean-stop",
         "after_identity": True, "disarm": False, "restart_of": None},
        {"name": "ready-exit1-restart", "arm": False, "pre_ready": False,
         "command": "exit0", "branch": "stock", "ready_phase": "blocked",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["0"]},
         "rc": "zero", "after_phase": "blocked", "after_reason": "unclean-stop",
         "after_identity": True, "disarm": False,
         "restart_of": "ready-exit1"},
        {"name": "disarm-while-ready", "arm": True, "pre_ready": False,
         "command": "exit0", "branch": "custom", "ready_phase": "ready",
         "finish": {"service_result": ["success"], "exit_code": ["exited"],
                    "exit_status": ["0"]},
         "rc": "zero", "after_phase": "blocked", "after_reason": "disarmed",
         "after_identity": False, "disarm": True, "restart_of": None},
    ]


class EventChannel:
    """One inherited AF_UNIX datagram socket for controller-side evidence."""

    def __init__(self, path):
        self.path = Path(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.socket.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.socket, selectors.EVENT_READ, "events")
        self.discarded = []

    def wait(self, kind, token, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            events = self.selector.select(remaining)
            if not events:
                return None
            for _key, _mask in events:
                try:
                    data = self.socket.recv(MAX_SNAPSHOT_BYTES)
                except BlockingIOError:
                    continue
                try:
                    payload = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._discard({"invalid_bytes": len(data)})
                    continue
                if (isinstance(payload, dict) and payload.get("kind") == kind
                        and payload.get("token") == token):
                    return payload
                self._discard({"kind": payload.get("kind") if isinstance(payload, dict) else None,
                               "token": payload.get("token") if isinstance(payload, dict) else None})

    def _discard(self, item):
        self.discarded.append(item)
        del self.discarded[:-20]

    def close(self):
        try:
            self.selector.close()
        finally:
            self.socket.close()


class Harness:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output)
        self.runtime = Path(args.runtime)
        self.runtime_base = (Path("/run/user") / str(os.getuid())).resolve()
        self.system_env = dict(os.environ)
        self.system_env["XDG_RUNTIME_DIR"] = str(self.runtime_base)
        self.keep_scratch = bool(args.keep_scratch)
        self.scratch = None
        self.fixture_root = None
        self.stock_dir = None
        self.fixture_shell = None
        self.stock_shell = None
        self.state = None
        self.ipc = None
        self.branch_dir = None
        self.hooks = None
        self.channel = None
        self.units = []
        self.processes = []
        self.cases = []
        self.case_map = {}
        self.lines = []
        self.setup_error = None
        self.harness_errors = []
        self.output_ready = False
        self.preflight = {}
        self.fixture = {}
        self.guard_path = self.runtime / "startup_guard.py"
        self.guard_reference = None
        self.guard_sha256 = None
        self.identity_keys = IDENTITY_FALLBACK
        self.codes = {}

    # -- logging / output -------------------------------------------------

    def log(self, message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        if len(line) > 1500:
            line = line[:1500] + "...(truncated)"
        self.lines.append(line)
        if len(self.lines) > MAX_JOURNAL_LINES:
            del self.lines[:len(self.lines) - MAX_JOURNAL_LINES]
        print(line, flush=True)

    def write_output(self, relative, payload):
        atomic_json(self.output / relative, payload)

    # -- command helpers --------------------------------------------------

    def command(self, argv, timeout=30, env=None):
        argv = [str(part) for part in argv]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True,
                                       timeout=timeout, check=False, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            self.log(f"command failed: {' '.join(argv)}: {type(error).__name__}")
            return None
        self.log(f"command: {' '.join(argv)} -> {completed.returncode}"
                 + (f" ({truncate(completed.stderr, 300).strip()})" if completed.returncode else ""))
        return completed

    def guard_env(self):
        return {"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C"}

    def production_cli(self, action):
        argv = [PYTHON, "-I", str(self.guard_path), action,
                "--runtime", str(self.fixture_root), "--mode", MODE,
                "--state", str(self.state)]
        completed = self.command(argv, timeout=30, env=self.guard_env())
        record = None
        if completed is not None and completed.stdout.strip():
            try:
                parsed = json.loads(completed.stdout.strip().splitlines()[-1])
                if isinstance(parsed, dict):
                    record = parsed
            except ValueError:
                record = None
        return {"argv": argv,
                "returncode": None if completed is None else completed.returncode,
                "stdout": truncate(None if completed is None else completed.stdout),
                "stderr": truncate(None if completed is None else completed.stderr),
                "record": record}

    def cli_detail(self, result):
        return {"returncode": result["returncode"],
                "record": record_summary(result["record"]) if result["record"] else None,
                "stderr": result["stderr"]}

    # -- state ------------------------------------------------------------

    def read_state(self):
        try:
            data = self.state.read_bytes()
        except FileNotFoundError:
            return {"phase": "missing"}
        except OSError:
            return {"phase": "unreadable"}
        if not data or len(data) > MAX_STATE_BYTES:
            return {"phase": "invalid"}
        try:
            record = json.loads(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return {"phase": "invalid"}
        if not isinstance(record, dict):
            return {"phase": "invalid"}
        return record

    # -- setup ------------------------------------------------------------

    def validate_environment(self):
        if not sys.platform.startswith("linux"):
            raise SetupError("this harness requires Linux; it cannot execute on this platform")
        if os.geteuid() == 0 or os.getuid() == 0:
            raise SetupError("run the harness as the desktop user, never as root")
        if os.getuid() != os.geteuid():
            raise SetupError("run the harness without setuid privileges")
        if not os.path.isabs(str(self.runtime)):
            raise SetupError("--runtime must be an absolute published runtime directory")
        try:
            resolved = self.runtime.resolve(strict=True)
        except OSError as error:
            raise SetupError("--runtime does not exist") from error
        if resolved != self.runtime or not resolved.is_dir():
            raise SetupError("--runtime must be a canonical absolute directory")
        if not self.guard_path.is_file() or not os.access(self.guard_path, os.R_OK):
            raise SetupError("--runtime does not contain a readable startup_guard.py")
        for tool in (PYTHON, CC, SYSTEMD_RUN, SYSTEMCTL):
            if not Path(tool).is_file() or not os.access(tool, os.X_OK):
                raise SetupError(f"required tool is unavailable: {tool}")
        try:
            info = self.runtime_base.lstat()
        except OSError as error:
            raise SetupError("the user runtime directory /run/user/UID is unavailable") from error
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SetupError("/run/user/UID is not a private, user-owned runtime directory")
        if not self.output.is_absolute():
            raise SetupError("--output must be absolute")
        if self.output.exists():
            raise SetupError("--output must be a NEW path; it already exists")
        try:
            self.output.mkdir(mode=0o700, parents=False, exist_ok=False)
            os.chmod(self.output, 0o700)
            self.output_ready = True
            (self.output / "cases").mkdir(mode=0o700)
        except OSError as error:
            raise SetupError(f"cannot create --output ({type(error).__name__})") from error

    def load_guard_reference(self):
        spec = importlib.util.spec_from_file_location(
            "socket_stream_guard_reference", self.guard_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["socket_stream_guard_reference"] = module
        try:
            spec.loader.exec_module(module)
        except Exception as error:
            raise SetupError(
                "production startup_guard.py could not be imported under isolated "
                f"semantics ({type(error).__name__})") from error
        self.guard_reference = module
        self.guard_sha256 = hashlib.sha256(self.guard_path.read_bytes()).hexdigest()
        self.identity_keys = tuple(getattr(module, "IDENTITY_KEYS", IDENTITY_FALLBACK))
        self.codes = {
            "blocked": getattr(module, "BLOCKED", "startup record is blocked"),
            "not-pending": getattr(module, "NOT_PENDING",
                                   "ready requires a matching pending startup"),
            "invocation-mismatch": getattr(module, "INVOCATION_MISMATCH",
                                           "INVOCATION_ID does not match the recorded startup"),
        }

    def build_scratch(self):
        base = tempfile.mkdtemp(prefix=PREFIX, dir=str(self.runtime_base))
        raw = Path(base)
        resolved = raw.resolve(strict=True)
        if resolved != raw:
            raise SetupError("the QA scratch directory path is not canonical")
        if not str(raw).startswith(str(self.runtime_base) + "/" + PREFIX):
            raise SetupError("the QA scratch directory is outside the expected user runtime tree")
        os.chmod(raw, 0o700)
        self.scratch = raw
        self.fixture_root = self.scratch / FIXTURE_ROOT_NAME
        self.stock_dir = self.scratch / "stock"
        self.ipc = self.scratch / "ipc"
        self.branch_dir = self.scratch / "branch"
        self.hooks = self.scratch / "hooks"
        for directory in (self.fixture_root, self.fixture_root / "lib",
                          self.stock_dir, self.ipc, self.branch_dir, self.hooks):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        self.materialize_libraries()
        self.compile_fixture()
        self.write_scripts()
        self.state = self.scratch / "guard.json"
        self.log(f"scratch: {self.scratch}")
        self.log(f"fixture runtime root: {self.fixture_root}")

    def materialize_libraries(self):
        libraries = {}
        for name in LIBRARIES:
            path = self.fixture_root / "lib" / name
            path.write_bytes(b"\0" * 4096)
            os.chmod(path, 0o444)
            info = path.stat()
            libraries[name] = {"path": str(path), "device": info.st_dev,
                               "inode": info.st_ino, "size": info.st_size}
        self.fixture["libraries"] = libraries
        self.fixture["library_root"] = str(self.fixture_root)

    def compile_fixture(self):
        build = self.scratch / "build"
        build.mkdir(mode=0o700, exist_ok=True)
        os.chmod(build, 0o700)
        source = build / "gnome-shell-fixture.c"
        source.write_text(FIXTURE_C, encoding="ascii")
        binary = build / "gnome-shell"
        completed = self.command([CC, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                  "-o", str(binary), str(source)], timeout=120)
        if completed is None or completed.returncode != 0:
            detail = "" if completed is None else truncate(completed.stderr)
            raise SetupError(f"the C fixture failed to compile: {detail}")
        self.fixture_shell = self.fixture_root / "gnome-shell"
        self.stock_shell = self.stock_dir / "gnome-shell"
        shutil.copyfile(binary, self.fixture_shell)
        shutil.copyfile(binary, self.stock_shell)
        os.chmod(self.fixture_shell, 0o755)
        os.chmod(self.stock_shell, 0o755)
        fixture_info = self.fixture_shell.stat()
        stock_info = self.stock_shell.stat()
        if (fixture_info.st_dev, fixture_info.st_ino) == (stock_info.st_dev, stock_info.st_ino):
            raise SetupError("the stock fixture must be a separate inode")
        self.fixture["gnome_shell"] = {"path": str(self.fixture_shell),
                                       "device": fixture_info.st_dev,
                                       "inode": fixture_info.st_ino}
        self.fixture["stock"] = {"path": str(self.stock_shell),
                                 "device": stock_info.st_dev,
                                 "inode": stock_info.st_ino}
        self.log(f"compiled fixture: {self.fixture_shell} "
                 f"(stock inode {stock_info.st_ino})")

    def write_scripts(self):
        scripts = {
            "launcher.py": LAUNCHER,
            "post_hook.py": POST_HOOK,
            "stop_hook.py": STOP_HOOK,
            "marker.py": MARKER_HOOK,
        }
        self.hook_paths = {}
        for name, source in scripts.items():
            try:
                compile(source, name, "exec")
            except SyntaxError as error:
                raise SetupError(f"embedded fixture script {name} is invalid") from error
            path = self.hooks / name
            path.write_text(source, encoding="ascii")
            os.chmod(path, 0o700)
            self.hook_paths[Path(name).stem] = path

    def start_channel(self):
        self.channel = EventChannel(self.ipc / "events.sock")

    # -- preflight --------------------------------------------------------

    def run_preflight(self):
        marker = self.hook_paths["marker"]
        token = os.urandom(6).hex()
        unit = self.make_unit("preflight-" + token)
        files = [self.ipc / f"preflight-{kind}-{index}.marker"
                 for index, kind in enumerate(("start-post-a", "start-post-b",
                                               "stop-post-a", "stop-post-b"))]
        properties = [
            ("Type", "oneshot"),
            ("TimeoutStartSec", str(START_TIMEOUT)),
            ("TimeoutStopSec", str(STOP_TIMEOUT)),
            ("KillMode", "control-group"),
            ("Restart", "no"),
            ("SuccessExitStatus", "1"),
            ("MemoryMax", "64M"),
            ("LimitCORE", "0"),
            ("ExecStartPost", self.hook_value([PYTHON, "-I", str(marker), str(files[0])])),
            ("ExecStartPost", self.hook_value([PYTHON, "-I", str(marker), str(files[1])])),
            ("ExecStopPost", self.hook_value([PYTHON, "-I", str(marker), str(files[2])])),
            ("ExecStopPost", self.hook_value([PYTHON, "-I", str(marker), str(files[3])])),
        ]
        argv = self.service_argv(unit, properties, ["/bin/true"])
        self.units.append(unit)
        completed = self.command(argv, timeout=START_TIMEOUT + STOP_TIMEOUT + WAIT_SLACK + 10,
                                 env=self.system_env)
        missing = [path.name for path in files if not path.is_file()]
        observed = None if completed is None else {
            "returncode": completed.returncode,
            "stdout": truncate(completed.stdout),
            "stderr": truncate(completed.stderr),
        }
        self.preflight = {"unit": unit, "markers": [path.name for path in files],
                          "missing": missing, "observed": observed}
        self.stop_unit(unit)
        if completed is None or completed.returncode != 0:
            raise SetupError("the systemd user manager could not run the preflight unit")
        if missing:
            raise SetupError(
                "systemd --user did not retain multiple ExecStartPost/ExecStopPost "
                f"entries (missing {missing}); this harness cannot establish hook order")
        self.log("preflight: multiple ExecStartPost/ExecStopPost entries retained")

    # -- service plumbing -------------------------------------------------

    def make_unit(self, slug):
        unit = PREFIX + slug + "-" + os.urandom(6).hex()
        if not UNIT_PATTERN.fullmatch(unit):
            raise SetupError("refusing to create an unrecognised unit name")
        return unit

    @staticmethod
    def hook_value(argv):
        return "-" + shlex.join([str(part) for part in argv])

    def service_argv(self, unit, properties, command):
        argv = [SYSTEMD_RUN, "--user", "--wait", "--pipe", "--unit", unit]
        for key, value in properties:
            argv.extend(["--property", key + "=" + value])
        argv.append("--")
        argv.extend([str(part) for part in command])
        return argv

    def service_properties(self, spec, slug, post_snapshot, stop_snapshot):
        guard_ready = [PYTHON, "-I", str(self.guard_path), "ready",
                       "--runtime", str(self.fixture_root), "--mode", MODE,
                       "--state", str(self.state)]
        guard_finish = [PYTHON, "-I", str(self.guard_path), "finish",
                        "--runtime", str(self.fixture_root), "--mode", MODE,
                        "--state", str(self.state)]
        post_hook = [PYTHON, "-I", str(self.hook_paths["post_hook"]),
                     str(self.guard_path), str(self.state),
                     str(self.ipc / "events.sock"), str(post_snapshot), slug,
                     str(self.fixture_root), str(self.fixture_shell),
                     str(self.stock_shell)]
        stop_hook = [PYTHON, "-I", str(self.hook_paths["stop_hook"]),
                     str(self.state), str(self.ipc / "events.sock"),
                     str(stop_snapshot), slug]
        return [
            ("Type", "notify"),
            ("NotifyAccess", "main"),
            ("Restart", "no"),
            ("SuccessExitStatus", "1"),
            ("TimeoutStartSec", str(START_TIMEOUT)),
            ("TimeoutStopSec", str(STOP_TIMEOUT)),
            ("RuntimeMaxSec", str(RUNTIME_MAX)),
            ("MemoryMax", "64M"),
            ("LimitCORE", "0"),
            ("KillMode", "control-group"),
            ("ExecStartPost", self.hook_value(guard_ready)),
            ("ExecStartPost", self.hook_value(post_hook)),
            ("ExecStopPost", self.hook_value(guard_finish)),
            ("ExecStopPost", self.hook_value(stop_hook)),
        ]

    def launcher_argv(self, spec, control_path):
        argv = [PYTHON, "-I", str(self.hook_paths["launcher"]),
                "--guard", str(self.guard_path), "--state", str(self.state),
                "--runtime", str(self.fixture_root), "--mode", MODE,
                "--custom", str(self.fixture_shell), "--stock", str(self.stock_shell),
                "--control", str(control_path), "--branch-dir", str(self.branch_dir),
                "--library-root", str(self.fixture_root)]
        if spec["pre_ready"]:
            argv.append("--exit-before-ready")
        return argv

    def start_service(self, unit, properties, command):
        argv = self.service_argv(unit, properties, command)
        self.log(f"start unit {unit}")
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=self.system_env)
        self.processes.append(process)
        return process

    def stop_unit(self, unit):
        if not UNIT_PATTERN.fullmatch(unit):
            raise SetupError("refusing to stop an unrecognised unit")
        self.command([SYSTEMCTL, "--user", "stop", unit],
                     timeout=STOP_TIMEOUT + 6, env=self.system_env)
        self.command([SYSTEMCTL, "--user", "reset-failed", unit],
                     timeout=6, env=self.system_env)

    def terminate_process(self, process):
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
            return
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass

    def finish_process(self, process):
        try:
            stdout, stderr = process.communicate(timeout=START_TIMEOUT + STOP_TIMEOUT + WAIT_SLACK + 4)
        except subprocess.TimeoutExpired as error:
            self.terminate_process(process)
            raise HarnessError("the service did not terminate within its bounded budget") from error
        return (truncate(stdout or "", MAX_OUTPUT_CHARS),
                truncate(stderr or "", MAX_OUTPUT_CHARS))

    def open_control(self, path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(1)
        return listener

    def accept_control(self, listener):
        listener.settimeout(ACCEPT_TIMEOUT)
        try:
            connection, _ = listener.accept()
        except (socket.timeout, OSError) as error:
            raise HarnessError(
                "the fixture main process did not connect to the control channel") from error
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", raw)
        except OSError:
            connection.close()
            raise HarnessError("the control channel credentials could not be read")
        return connection, {"pid": pid, "uid": uid, "gid": gid}

    def send_command(self, connection, command):
        try:
            connection.sendall(command.encode("ascii") + b"\n")
        except OSError as error:
            raise HarnessError("the fixture control command could not be delivered") from error
        finally:
            connection.close()

    def load_snapshot(self, path):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        if not data or len(data) > MAX_SNAPSHOT_BYTES:
            return None
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def snapshot(self, path, event):
        payload = self.load_snapshot(path)
        if payload is None and isinstance(event, dict):
            payload = event.get("data")
        if not isinstance(payload, dict):
            raise HarnessError("fixture evidence snapshot is missing or invalid")
        return payload

    # -- case execution ---------------------------------------------------

    def add_check(self, case, name, ok, detail=None, informational=False):
        entry = {"name": name, "ok": None if informational else bool(ok)}
        if informational:
            entry["informational"] = True
        if detail is not None:
            entry["detail"] = detail
        case["checks"].append(entry)

    @staticmethod
    def failed(case):
        return any(entry.get("ok") is False for entry in case["checks"])

    def load_branch(self, invocation, post, stop):
        identifier = invocation or (post or {}).get("invocation_id") or (stop or {}).get("invocation_id")
        if not identifier:
            return None
        name = "".join(character for character in str(identifier)
                       if character.isalnum() or character in "-_")
        if not name:
            return None
        payload = self.load_snapshot(self.branch_dir / (name + ".json"))
        if payload is None:
            payload = self.load_snapshot(self.branch_dir / "unknown.json")
        return payload

    def diagnostics(self, output):
        reasons = re.findall(r"SocketStream guard: ([^\r\n]+)", output or "")
        records = []
        for line in (output or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("{") or len(stripped) > 900:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("phase") in PHASES:
                records.append({"phase": record.get("phase"),
                                "reason": record.get("reason")})
        return {"guard_reasons": reasons[-12:], "guard_records": records[-12:]}

    def run_case(self, spec, previous):
        case = {
            "name": spec["name"],
            "slug": self.slug(spec["name"]),
            "status": "pending",
            "expected": {key: spec[key] for key in
                         ("arm", "pre_ready", "command", "branch", "ready_phase",
                          "finish", "rc", "after_phase", "after_reason",
                          "after_identity", "disarm", "restart_of")},
            "checks": [],
            "service": {},
            "state": {},
            "branch": None,
            "diagnostics": {},
        }
        self.cases.append(case)
        self.case_map[spec["name"]] = case
        self.log(f"case {spec['name']}: start")
        started = time.monotonic()
        try:
            self.execute_case(case, spec, previous)
        except CaseAborted as error:
            case["status"] = "fail"
            case["error"] = str(error)
            self.log(f"case {spec['name']}: failed early: {error}")
        except HarnessError as error:
            case["status"] = "error"
            case["error"] = str(error)
            self.harness_errors.append({"case": spec["name"], "error": str(error)})
            self.log(f"case {spec['name']}: harness error: {error}")
        except Exception as error:  # noqa: BLE001 - every failure must be reported
            case["status"] = "error"
            case["error"] = f"{type(error).__name__}: {error}"
            self.harness_errors.append({"case": spec["name"], "error": case["error"]})
            self.log(f"case {spec['name']}: unexpected harness error: {case['error']}")
        finally:
            self.close_case(case)
            if case["status"] == "pending":
                case["status"] = "fail" if self.failed(case) else "pass"
            case["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.log(f"case {spec['name']}: {case['status']}")
            self.write_output(Path("cases") / (case["slug"] + ".json"), case)
        return case

    @staticmethod
    def slug(name):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug or "case"

    def close_case(self, case):
        process = case.pop("_process", None)
        listener = case.pop("_listener", None)
        connection = case.pop("_connection", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        unit = case.get("service", {}).get("unit")
        if process is not None and process.poll() is None:
            if unit:
                self.stop_unit(unit)
            try:
                stdout, stderr = process.communicate(timeout=STOP_TIMEOUT + WAIT_SLACK)
                service = case.setdefault("service", {})
                service["aborted_output"] = truncate((stdout or "") + "\n" + (stderr or ""))
                if not case.get("diagnostics"):
                    case["diagnostics"] = self.diagnostics(service["aborted_output"])
            except (subprocess.SubprocessError, OSError):
                self.terminate_process(process)
        elif unit:
            self.stop_unit(unit)

    def execute_case(self, case, spec, previous):
        before = self.read_state()
        case["state"]["before"] = record_summary(before)
        if spec["arm"]:
            armed = self.production_cli("arm")
            case["arm"] = self.cli_detail(armed)
            ok = (armed["returncode"] == 0 and isinstance(armed["record"], dict)
                  and armed["record"].get("phase") == "armed")
            self.add_check(case, "guard-arm", ok, self.cli_detail(armed))
            if not ok:
                raise CaseAborted("the production arm action did not report armed")
        elif previous is not None:
            previous_phase = (previous.get("state", {}).get("after") or {}).get("phase")
            self.add_check(case, "state-before-is-stale-blocked",
                           before.get("phase") == "blocked" and previous_phase == "blocked",
                           {"before": case["state"]["before"],
                            "previous_after": previous.get("state", {}).get("after")})
            if before.get("phase") != "blocked":
                raise CaseAborted("the failure record was not blocked before the restart")

        unit = self.make_unit(self.slug(spec["name"]))
        case["service"]["unit"] = unit
        self.units.append(unit)
        slug = case["slug"]
        post_snapshot = self.ipc / f"post-{slug}.json"
        stop_snapshot = self.ipc / f"stop-{slug}.json"
        control_path = self.ipc / f"control-{slug}.sock"
        listener = self.open_control(control_path)
        case["_listener"] = listener
        properties = self.service_properties(spec, slug, post_snapshot, stop_snapshot)
        case["service"]["properties"] = {
            key: [value for name, value in properties if name == key]
            for key in dict.fromkeys(name for name, _ in properties)}

        process = self.start_service(unit, properties, self.launcher_argv(spec, control_path))
        case["_process"] = process
        post = None
        stop_event = None
        if spec["pre_ready"]:
            stop_event = self.channel.wait("stop", slug,
                                           START_TIMEOUT + STOP_TIMEOUT + WAIT_SLACK)
        else:
            post_event = self.channel.wait("post", slug,
                                           START_TIMEOUT + STOP_TIMEOUT + WAIT_SLACK)
            if post_event is None:
                post = self.load_snapshot(post_snapshot)
                if not isinstance(post, dict):
                    raise HarnessError(
                        "ExecStartPost evidence did not arrive; the controller refused "
                        "to induce an exit before observing post-hook state")
                self.add_check(case, "post-datagram",
                               False, "snapshot file was present; datagram was not",
                               informational=True)
            else:
                post = self.snapshot(post_snapshot, post_event)
            case["service"]["post"] = post
            post_phase = (post.get("state") or {}).get("phase")
            self.add_check(case, "exec-start-post-phase",
                           post_phase == spec["ready_phase"],
                           {"observed": post_phase, "expected": spec["ready_phase"]})
            if post_phase != spec["ready_phase"]:
                raise CaseAborted(
                    "the guard state after ExecStartPost did not match the expected phase")
            connection, peer = self.accept_control(listener)
            case["_connection"] = connection
            case["service"]["control_peer"] = peer
            self.add_check(case, "control-peer-uid", peer["uid"] == os.getuid(), peer)
            if peer["uid"] != os.getuid():
                raise CaseAborted("the fixture control channel came from another user")
            if spec["disarm"]:
                self.disarm_case(case)
            self.send_command(connection, spec["command"])
            case["_connection"] = None
            stop_event = self.channel.wait("stop", slug, STOP_TIMEOUT + WAIT_SLACK)

        stdout, stderr = self.finish_process(process)
        service = case["service"]
        service["systemd_run_rc"] = process.returncode
        service["stdout"] = stdout
        service["stderr"] = stderr
        service["output_bytes"] = len(stdout) + len(stderr)
        stop = self.snapshot(stop_snapshot, stop_event)
        service["stop"] = stop
        case["diagnostics"] = self.diagnostics((stdout or "") + "\n" + (stderr or ""))
        branch = self.load_branch(None, post, stop)
        case["branch"] = branch
        self.analyze_case(case, spec, previous, post, stop, branch)

    def disarm_case(self, case):
        main_pid = (case.get("service", {}).get("control_peer") or {}).get("pid")
        alive = bool(main_pid) and (Path("/proc") / str(main_pid)).is_dir()
        self.add_check(case, "main-alive-during-disarm", alive, main_pid)
        disarm = self.production_cli("disarm")
        case["disarm"] = self.cli_detail(disarm)
        ok = (disarm["returncode"] == 0 and isinstance(disarm["record"], dict)
              and disarm["record"].get("phase") == "blocked"
              and disarm["record"].get("reason") == "disarmed")
        self.add_check(case, "guard-disarm", ok, self.cli_detail(disarm))
        after_disarm = self.read_state()
        case["state"]["after_disarm"] = record_summary(after_disarm)
        self.add_check(case, "disarm-marker-while-live",
                       after_disarm.get("phase") == "blocked"
                       and after_disarm.get("reason") == "disarmed"
                       and not any(key in after_disarm for key in self.identity_keys),
                       case["state"]["after_disarm"])
        if not ok:
            raise CaseAborted("the production disarm action did not leave a blocked marker")

    # -- analysis ---------------------------------------------------------

    def analyze_case(self, case, spec, previous, post, stop, branch):
        service = case["service"]
        peer = service.get("control_peer")
        actual = {
            "service_result": stop.get("service_result"),
            "exit_code": stop.get("exit_code"),
            "exit_status": stop.get("exit_status"),
        }
        service["finish_tuple"] = actual
        service["result"] = actual["service_result"]
        service["exec_main_code"] = actual["exit_code"]
        service["exec_main_status"] = actual["exit_status"]
        for key, allowed in spec["finish"].items():
            self.add_check(case, "finish-" + key, actual.get(key) in allowed,
                           {"observed": actual.get(key), "expected": allowed})

        rc = service.get("systemd_run_rc")
        if spec["rc"] == "zero":
            self.add_check(case, "systemd-run-rc", rc == 0, rc)
        elif spec["rc"] == "nonzero":
            self.add_check(case, "systemd-run-rc", rc not in (0, None), rc)

        invocation = stop.get("invocation_id") or (post or {}).get("invocation_id")
        state = self.read_state()
        case["state"]["after"] = record_summary(state)
        status = self.production_cli("status")
        case["state"]["status_cli"] = self.cli_detail(status)
        self.add_check(case, "status-cli",
                       status["returncode"] == 0 and isinstance(status["record"], dict)
                       and status["record"].get("phase") == state.get("phase"),
                       self.cli_detail(status))
        self.add_check(case, "state-after-phase",
                       state.get("phase") == spec["after_phase"],
                       {"observed": case["state"]["after"],
                        "expected_phase": spec["after_phase"]})
        if spec["after_reason"] is not None:
            self.add_check(case, "state-after-reason",
                           state.get("reason") == spec["after_reason"],
                           {"observed": state.get("reason"),
                            "expected": spec["after_reason"]})
        identity_present = all(key in state for key in self.identity_keys)
        self.add_check(case, "state-after-identity",
                       identity_present == spec["after_identity"],
                       {"present": identity_present,
                        "expected": spec["after_identity"]})
        if state.get("phase") == "armed":
            self.add_check(case, "status-identity",
                           (status.get("record") or {}).get("identity") == "same",
                           (status.get("record") or {}).get("identity"))

        if post is not None:
            self.add_check(case, "helper-environment-without-optin",
                           post.get("helper_optin") is False, post.get("helper_optin"))
            self.add_check(case, "main-owner",
                           post.get("process_owner") == os.getuid(),
                           post.get("process_owner"))
            exported = post.get("main_environment") or {}
            if spec["branch"] == "custom":
                self.add_check(case, "main-process-optin",
                               exported.get("SOCKET_STREAM_CAPTURE_EXCLUDE") == "1",
                               {"environment": exported,
                                "error": post.get("main_environment_error")})
            else:
                self.add_check(case, "stock-process-without-optin",
                               "SOCKET_STREAM_CAPTURE_EXCLUDE" not in exported,
                               {"environment": exported,
                                "error": post.get("main_environment_error")})
            self.add_check(case, "main-invocation",
                           exported.get("INVOCATION_ID") == post.get("invocation_id"),
                           {"environment_invocation": exported.get("INVOCATION_ID"),
                            "service_invocation": post.get("invocation_id")})
            if spec["branch"] == "custom":
                self.add_check(case, "main-executable",
                               post.get("main_is_fixture") is True
                               and post.get("main_is_stock") is False,
                               {"fixture": post.get("main_is_fixture"),
                                "stock": post.get("main_is_stock"),
                                "executable": post.get("process_executable")})
                post_state = post.get("state") or {}
                if "starttime" in post_state:
                    self.add_check(case, "starttime-agreement",
                                   post_state.get("starttime") == post.get("process_starttime"),
                                   {"state_starttime": post_state.get("starttime"),
                                    "proc_starttime": post.get("process_starttime")})
                mappings = post.get("mutter_mappings") or {}
                self.add_check(case, "mutter-mappings",
                               mappings.get("complete") is True,
                               {"complete": mappings.get("complete"),
                                "entries": mappings.get("entries"),
                                "expected_paths": mappings.get("expected_paths")})
            else:
                self.add_check(case, "main-executable",
                               post.get("main_is_stock") is True
                               and post.get("main_is_fixture") is False,
                               {"fixture": post.get("main_is_fixture"),
                                "stock": post.get("main_is_stock"),
                                "executable": post.get("process_executable")})
                mappings = post.get("mutter_mappings") or {}
                self.add_check(case, "no-mutter-mappings",
                               mappings.get("entries") == [],
                               mappings.get("entries"))

        self.add_check(case, "stop-helper-without-optin",
                       stop.get("helper_optin") is False, stop.get("helper_optin"))

        if branch is None:
            self.add_check(case, "launcher-branch", False, "no launcher branch record")
        else:
            self.add_check(case, "launcher-branch",
                           branch.get("branch") == spec["branch"],
                           {"branch": branch.get("branch"),
                            "detail": branch.get("detail"),
                            "reason": branch.get("reason"),
                            "error": branch.get("error")})
            if spec["branch"] == "custom":
                self.add_check(case, "consume-refusal-none",
                               branch.get("detail") == "guard-claimed",
                               branch.get("detail"))
            else:
                self.add_check(case, "consume-refusal-blocked",
                               branch.get("detail") == "guard-refused"
                               and branch.get("reason") == self.codes["blocked"],
                               {"detail": branch.get("detail"),
                                "reason": branch.get("reason"),
                                "expected": self.codes["blocked"]})

        self.check_pids(case, spec, post, stop, peer, branch, state)

        if previous is not None:
            previous_after = previous.get("state", {}).get("after") or {}
            self.add_check(case, "new-systemd-invocation",
                           invocation is not None
                           and invocation != previous_after.get("invocation_id"),
                           {"observed": invocation,
                            "previous": previous_after.get("invocation_id")})
            retained = (state.get("invocation_id") == previous_after.get("invocation_id")
                        and state.get("pid") == previous_after.get("pid")
                        and state.get("starttime") == previous_after.get("starttime"))
            self.add_check(case, "stale-record-retained", retained,
                           {"observed": case["state"]["after"],
                            "previous": previous_after})
            reasons = case["diagnostics"].get("guard_reasons") or []
            self.add_check(case, "ready-hook-refusal",
                           self.codes["not-pending"] in reasons, reasons,
                           informational=True)

        if spec["disarm"]:
            after_disarm = case["state"].get("after_disarm") or {}
            self.add_check(case, "late-clean-finish-no-rearm",
                           state.get("phase") == "blocked"
                           and state.get("reason") == "disarmed",
                           case["state"]["after"])
            self.add_check(case, "late-clean-finish-no-identity",
                           not any(key in state for key in self.identity_keys),
                           case["state"]["after"])
            reasons = case["diagnostics"].get("guard_reasons") or []
            self.add_check(case, "finish-invocation-mismatch",
                           self.codes["invocation-mismatch"] in reasons, reasons,
                           informational=True)

        if spec["branch"] == "custom" and post is not None:
            records = case["diagnostics"].get("guard_records") or []
            self.add_check(case, "ready-hook-record",
                           any(record.get("phase") == "ready" for record in records),
                           records, informational=True)

    def check_pids(self, case, spec, post, stop, peer, branch, state):
        values = {
            "stop_main_pid": self.integer(stop.get("main_pid")),
        }
        if post is not None:
            values["post_main_pid"] = self.integer(post.get("main_pid"))
        if peer is not None:
            values["control_peer_pid"] = peer.get("pid")
        if branch is not None:
            values["launcher_pid"] = self.integer(branch.get("pid"))
        if spec["after_identity"] and spec["restart_of"] is None:
            values["stored_pid"] = self.integer(state.get("pid"))
        usable = {key: value for key, value in values.items() if value is not None}
        required = []
        if branch is not None:
            required.append("launcher_pid")
        if post is not None:
            required.append("post_main_pid")
        if peer is not None:
            required.append("control_peer_pid")
        if spec["pre_ready"]:
            required.append("stored_pid")
        agreement = (bool(required)
                     and all(values.get(key) not in (None, 0) for key in required)
                     and len(set(usable.values())) == 1
                     and all(value > 0 for value in usable.values()))
        self.add_check(case, "main-pid-agreement", agreement,
                       {"values": values, "required": required,
                        "stored": case["state"]["after"]})
        if branch is not None and spec["branch"] == "custom":
            claim = self.integer(branch.get("claim_pid"))
            target = (values.get("post_main_pid") or values.get("stop_main_pid")
                      or values.get("stored_pid"))
            self.add_check(case, "consume-pid-equals-main",
                           claim is not None and claim == target,
                           {"consume_pid": claim, "service_main_pid": target})

    @staticmethod
    def integer(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    # -- orchestration ----------------------------------------------------

    def run_all(self):
        specs = build_specs(self.args.crash_signal)
        for spec in specs:
            previous = None
            if spec["restart_of"]:
                previous = self.case_map.get(spec["restart_of"])
                if previous is None or previous["status"] != "pass":
                    case = {
                        "name": spec["name"],
                        "slug": self.slug(spec["name"]),
                        "status": "skipped",
                        "reason": ("prerequisite case did not pass; the stale-blocked "
                                   "restart cannot be judged"),
                        "expected": {key: spec[key] for key in
                                     ("arm", "pre_ready", "command", "branch",
                                      "ready_phase", "finish", "rc", "after_phase",
                                      "after_reason", "after_identity", "disarm",
                                      "restart_of")},
                        "checks": [],
                    }
                    self.cases.append(case)
                    self.case_map[spec["name"]] = case
                    self.log(f"case {spec['name']}: skipped (prerequisite failed)")
                    self.write_output(Path("cases") / (case["slug"] + ".json"), case)
                    continue
            self.run_case(spec, previous)

    def cleanup(self):
        if self.channel is not None:
            try:
                self.channel.close()
            except OSError:
                pass
            self.channel = None
        for process in list(self.processes):
            self.terminate_process(process)
        for unit in list(self.units):
            if UNIT_PATTERN.fullmatch(unit):
                self.command([SYSTEMCTL, "--user", "stop", unit],
                             timeout=STOP_TIMEOUT + 6, env=self.system_env)
                self.command([SYSTEMCTL, "--user", "reset-failed", unit],
                             timeout=6, env=self.system_env)
        if self.scratch is not None and not self.keep_scratch:
            text = str(self.scratch)
            if text.startswith(str(self.runtime_base) + "/" + PREFIX):
                shutil.rmtree(self.scratch)
            else:
                raise SetupError("refusing to remove a scratch path outside the QA prefix")
        if self.keep_scratch and self.scratch is not None:
            self.log(f"scratch retained at {self.scratch}")

    def summary(self):
        harness_error = self.setup_error is not None or bool(self.harness_errors)
        failed = [case for case in self.cases if case.get("status") == "fail"]
        skipped = [case for case in self.cases if case.get("status") == "skipped"]
        errored = [case for case in self.cases if case.get("status") == "error"]
        if self.setup_error:
            status = "setup-error"
        elif harness_error or errored:
            status = "harness-error"
        elif failed:
            status = "fail"
        else:
            status = "pass"
        return {
            "schema": 1,
            "kind": "mutter-startup-guard-qa",
            "generated_utc": utc_now(),
            "status": status,
            "uid": os.getuid(),
            "euid": os.geteuid(),
            "runtime": str(self.runtime),
            "guard_sha256": self.guard_sha256,
            "mode": MODE,
            "fixture": self.fixture,
            "service": {
                "unit_prefix": PREFIX,
                "timeouts": {"start": START_TIMEOUT, "stop": STOP_TIMEOUT,
                             "runtime_max": RUNTIME_MAX},
                "properties": {
                    "Type": "notify", "NotifyAccess": "main", "Restart": "no",
                    "SuccessExitStatus": "1", "MemoryMax": "64M", "LimitCORE": "0",
                    "KillMode": "control-group",
                },
                "finish_tuple_source": (
                    "SERVICE_RESULT, EXIT_CODE and EXIT_STATUS captured from systemd's "
                    "ExecStopPost environment; these are the Result, ExecMainCode and "
                    "ExecMainStatus values, recorded as observed and never inferred"),
            },
            "preflight": self.preflight,
            "cases": self.cases,
            "setup_error": self.setup_error,
            "harness_errors": self.harness_errors,
            "failed_cases": [case["name"] for case in failed],
            "skipped_cases": [case["name"] for case in skipped],
            "errored_cases": [case["name"] for case in errored],
            "scratch_retained": bool(self.keep_scratch),
            "scratch": str(self.scratch) if self.keep_scratch and self.scratch else None,
            "limits": HARNESS_LIMITS,
        }

    def finish(self):
        try:
            self.cleanup()
        except Exception as error:  # noqa: BLE001 - cleanup must not hide the result
            self.harness_errors.append(
                {"case": None, "error": f"cleanup: {type(error).__name__}: {error}"})
        summary = self.summary()
        self.log(f"result: {summary['status']} ({len(self.cases)} cases)")
        for case in self.cases:
            detail = ""
            if case.get("error"):
                detail = " - " + case["error"]
            elif case.get("reason"):
                detail = " - " + case["reason"]
            self.log(f"  {case['status']:8} {case['name']}{detail}")
        self.log("evidence: " + (str(self.output / "summary.json")
                                 if self.output_ready else "not written"))
        try:
            if not self.output_ready:
                raise OSError("the requested --output was not accepted; refusing to write into it")
            self.write_output("summary.json", summary)
            (self.output / "harness.log").write_text(
                "\n".join(self.lines) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"evidence could not be written ({error})", file=sys.stderr)
            print(json.dumps(json_safe(summary), indent=2, sort_keys=True),
                  file=sys.stderr)
        if summary["status"] == "pass":
            return 0
        if summary["status"] == "fail":
            return 1
        return 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Real-Linux systemd/proc integration QA for startup_guard.py")
    parser.add_argument("--runtime", required=True,
                        help="absolute published runtime root supplying startup_guard.py")
    parser.add_argument("--output", required=True,
                        help="NEW absolute evidence directory (created 0700)")
    parser.add_argument("--crash-signal", choices=("segv", "kill"), default="segv",
                        help="post-readiness crash signal (default: segv)")
    parser.add_argument("--keep-scratch", action="store_true",
                        help="keep the 0700 /run/user/UID QA scratch tree for debugging")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    harness = Harness(args)
    try:
        harness.validate_environment()
        harness.load_guard_reference()
        harness.build_scratch()
        harness.start_channel()
        harness.run_preflight()
        harness.run_all()
    except KeyboardInterrupt:
        harness.harness_errors.append({"case": None, "error": "interrupted by the user"})
        harness.log("harness interrupted; bounded cleanup follows")
    except SetupError as error:
        harness.setup_error = str(error)
        harness.log(f"setup error: {error}")
    except HarnessError as error:
        harness.harness_errors.append({"case": None, "error": str(error)})
        harness.log(f"harness error: {error}")
    except Exception as error:  # noqa: BLE001 - never hide an infrastructure failure
        harness.harness_errors.append(
            {"case": None, "error": f"{type(error).__name__}: {error}"})
        harness.log(f"harness error: {type(error).__name__}: {error}")
    return harness.finish()


if __name__ == "__main__":
    raise SystemExit(main())
