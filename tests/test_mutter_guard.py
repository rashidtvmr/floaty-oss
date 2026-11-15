"""One-shot startup guard: durable claim, phase protocol, hostile paths.

No compositor, systemd service, or real ``/proc`` is required. Process and boot
identity come from a fixture tree; concurrency and power-loss cases run in fresh
helper interpreter processes; the CLI is exercised where it needs no live
process, and its consume path is checked only where a real ``/proc`` exists.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

NATIVE = Path(__file__).resolve().parents[1] / "native/mutter-capture-filter"
sys.path.insert(0, str(NATIVE))
import startup_guard as guard
sys.path.pop(0)

BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "99999999-8888-7777-6666-555555555555"
INVOCATION = "a1" * 16
OTHER_INVOCATION = "b2" * 16
PID = 4242
OTHER_PID = 4343
START = 987654
OTHER_START = 123456
MODE = "ubuntu"
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
CLI = NATIVE / "startup_guard.py"
LIBRARIES = guard.MAPPING_LIBRARIES


class _ForeignOwnerProc(guard.Proc):
    """Test seam: /proc/PID owned by a different UID (needs root to build)."""

    def owner(self, pid):
        value = super().owner(pid)
        return None if value is None else value + 1

HELPER = '''\
import json, sys
sys.path.insert(0, sys.argv[1])
import startup_guard as guard
from pathlib import Path

proc = guard.Proc(root=Path(sys.argv[2]), boot_id_path=Path(sys.argv[3]))
action = sys.argv[4]
state, root, mode = Path(sys.argv[5]), Path(sys.argv[6]), sys.argv[7]
environment = json.loads(sys.argv[8])
pid = int(sys.argv[9]) if sys.argv[9] else None
try:
    if action == "consume":
        record = guard.consume(state, root, mode, environ=environment, pid=pid, proc=proc)
    elif action == "ready":
        record = guard.ready(state, root, mode, environ=environment, proc=proc)
    elif action == "finish":
        record = guard.finish(state, root, mode, environ=environment, proc=proc)
    elif action == "status":
        record = guard.status(state, root, mode)
    else:
        record = getattr(guard, action)(state, root, mode, proc=proc)
except guard.GuardError as error:
    print(json.dumps({"error": error.code}, sort_keys=True))
    raise SystemExit(3)
print(json.dumps(record, sort_keys=True))
'''

UMASK_HELPER = '''\
import json, os, sys
os.umask(0o277)
sys.path.insert(0, sys.argv[1])
import startup_guard as guard
from pathlib import Path

proc = guard.Proc(root=Path(sys.argv[2]), boot_id_path=Path(sys.argv[3]))
state, root, mode = Path(sys.argv[4]), Path(sys.argv[5]), sys.argv[6]
invocation, pid = sys.argv[7], int(sys.argv[8])
guard.arm(state, root, mode, proc=proc)
guard.consume(state, root, mode, environ={"INVOCATION_ID": invocation},
              pid=pid, proc=proc)
record = guard.ready(state, root, mode, environ={
    "INVOCATION_ID": invocation, "MAINPID": str(pid)}, proc=proc)
print(json.dumps(record, sort_keys=True))
'''


class GuardFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.uid = os.getuid()
        self.root = self.base / "50.1.socket-stream.0123456789abcdef"
        self.root.mkdir()
        self.shell = self.root / "gnome-shell"
        self.shell.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.shell.chmod(0o444)
        self.proc_root = self.base / "proc"
        self.proc_root.mkdir()
        self.boot_file = self.base / "boot_id"
        self.boot_file.write_text(BOOT + "\n", encoding="ascii")
        (self.base / "config" / "socket_stream").mkdir(parents=True)
        self.state_dir = self.base / "config/socket_stream/mutter-startup"
        self.state = self.state_dir / ("0123456789abcdef0123456789abcdef.json")
        self.proc = guard.Proc(root=self.proc_root, boot_id_path=self.boot_file)
        self.helper = self.base / "helper.py"
        self.helper.write_text(HELPER, encoding="utf-8")
        self.start_process(PID, START)
        self.make_libraries()
        self.write_maps()

    # -- fixtures ---------------------------------------------------------

    def make_libraries(self):
        """Real private library files so the mapping check has real inodes."""
        (self.root / "lib").mkdir(exist_ok=True)
        for name in LIBRARIES:
            (self.root / name).write_bytes(b"\x7fELF\n")

    def mapping_line(self, file, *, pathname=None, inode=None, device=None,
                     deleted=False):
        """One /proc/PID/maps segment pointing at ``file`` by default."""
        info = file.stat()
        if device is None:
            device = "%02x:%02x" % (os.major(info.st_dev), os.minor(info.st_dev))
        if inode is None:
            inode = info.st_ino
        name = str(file) if pathname is None else pathname
        if deleted:
            name += " (deleted)"
        return "7f0000000000-7f0000001000 r-xp 00000000 %s %d %s\n" % (
            device, inode, name)

    def default_maps(self):
        return [self.mapping_line(self.root / name) for name in LIBRARIES]

    def write_maps(self, pid=PID, lines=None):
        directory = self.proc_root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        if lines is None:
            lines = self.default_maps()
        (directory / "maps").write_text("".join(lines), encoding="ascii")

    def start_process(self, pid=PID, starttime=START, exe=None, environ=None):
        directory = self.proc_root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        fields = ["S"] + ["0"] * 18 + [str(starttime)] + ["0"] * 20
        (directory / "stat").write_text(
            "%d (gnome-shell) %s\n" % (pid, " ".join(fields)), encoding="ascii")
        values = {"SOCKET_STREAM_CAPTURE_EXCLUDE": "1", "INVOCATION_ID": INVOCATION}
        if environ is not None:
            values = dict(environ)
        (directory / "environ").write_bytes(
            b"".join(f"{key}={value}".encode("ascii") + b"\0"
                     for key, value in values.items()))
        link = directory / "exe"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(exe or self.shell)

    def kill_process(self, pid=PID):
        shutil.rmtree(self.proc_root / str(pid), ignore_errors=True)

    def environment(self, **extra):
        values = {"INVOCATION_ID": INVOCATION}
        values.update(extra)
        return values

    def ready_environment(self, **extra):
        # The systemd helper's own environment: it never carries the opt-in,
        # which the launcher exports only into the compositor it execs.
        values = self.environment(MAINPID=str(PID))
        values.update(extra)
        return values

    # -- actions ----------------------------------------------------------

    def arm(self):
        return guard.arm(self.state, self.root, MODE, proc=self.proc)

    def consume(self, pid=PID, environ=None, proc=None):
        return guard.consume(
            self.state, self.root, MODE,
            environ=self.environment() if environ is None else environ,
            pid=pid, proc=proc or self.proc)

    def ready(self, environ=None, proc=None):
        return guard.ready(
            self.state, self.root, MODE,
            environ=self.ready_environment() if environ is None else environ,
            proc=proc or self.proc)

    def finish(self, **extra):
        values = self.environment(**extra)
        return guard.finish(self.state, self.root, MODE, environ=values, proc=self.proc)

    def status(self):
        return guard.status(self.state, self.root, MODE, proc=self.proc)

    def stored(self):
        return json.loads(self.state.read_text(encoding="ascii"))

    def stored_bytes(self):
        return self.state.read_bytes()

    def helper_process(self, action, *, environ=None, pid=None):
        argv = [
            sys.executable, str(self.helper), str(NATIVE), str(self.proc_root),
            str(self.boot_file), action, str(self.state), str(self.root), MODE,
            json.dumps(environ or {}), "" if pid is None else str(pid),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        return result

    def helper_action(self, action, *, environ=None, pid=None):
        result = self.helper_process(action, environ=environ, pid=pid)
        self.assertIn(result.returncode, (0, 3), result.stderr)
        payload = json.loads(result.stdout)
        if result.returncode:
            raise guard.GuardError(payload["error"])
        return payload

    def cli(self, action, *, state=None, runtime=None, mode=MODE, environ=None, extra=()):
        argv = [
            PYTHON, "-I", str(CLI), action,
            "--runtime", str(runtime if runtime is not None else self.root),
            "--mode", mode,
            "--state", str(state if state is not None else self.state),
            *extra,
        ]
        environment = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
        environment.update(environ or {})
        return subprocess.run(argv, capture_output=True, text=True, timeout=120,
                              env=environment, check=False)

    def assertRefused(self, code, action, *args, **kwargs):
        with self.assertRaises(guard.GuardError) as caught:
            action(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class CleanCycleTests(GuardFixture):
    def test_clean_login_cycle_is_durable_and_private(self):
        self.assertEqual(self.status()["phase"], "missing")
        self.assertEqual(self.arm()["phase"], "armed")
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        lock = self.state_dir / guard.lock_name(self.state)
        self.assertTrue(lock.is_file())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

        pending = self.consume()
        self.assertEqual(pending, self.stored())
        self.assertEqual(pending["phase"], "pending")
        self.assertEqual(pending["invocation_id"], INVOCATION)
        self.assertEqual(pending["boot_id"], BOOT)
        self.assertEqual(pending["pid"], PID)
        self.assertEqual(pending["starttime"], START)

        ready = self.ready()
        self.assertEqual(ready["phase"], "ready")
        self.assertEqual(self.stored(), ready)

        rearmed = self.finish(SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(rearmed["phase"], "armed")
        self.assertEqual(rearmed["reason"], "clean-stop")
        for key in guard.IDENTITY_KEYS:
            self.assertNotIn(key, rearmed)
        self.assertEqual(self.stored(), rearmed)

        # A later login consumes the re-armed record exactly once.
        again = self.consume(environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))
        self.assertEqual(again["invocation_id"], OTHER_INVOCATION)

    def test_arm_is_idempotent_and_replaces_only_non_live_records(self):
        self.arm()
        self.assertEqual(self.arm(), {"schema": 1, "runtime": str(self.root),
                                      "mode": MODE, "phase": "armed"})
        self.start_process(OTHER_PID, OTHER_START)
        self.consume(pid=OTHER_PID)
        self.assertRefused(guard.STILL_LIVE, self.arm)

    def test_arm_refuses_live_pending_record_for_launcher_process(self):
        # consume happens before exec, so the live PID is not yet gnome-shell.
        self.arm()
        self.consume()
        self.assertRefused(guard.STILL_LIVE, self.arm)
        # Same PID with a new start time is PID reuse: the old claim is stale.
        self.start_process(PID, START + 1)
        self.assertEqual(self.arm()["phase"], "armed")
        # A dead process releases the claim as well.
        self.start_process(OTHER_PID, OTHER_START)
        self.consume(pid=OTHER_PID)
        self.kill_process(OTHER_PID)
        self.assertEqual(self.arm()["phase"], "armed")


class AtomicClaimTests(GuardFixture):
    def test_concurrent_consume_claims_once_in_process(self):
        self.arm()
        self.start_process(OTHER_PID, OTHER_START)
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(pid):
            barrier.wait()
            try:
                record = self.consume(pid=pid)
            except guard.GuardError as error:
                with lock:
                    outcomes.append(error.code)
            else:
                with lock:
                    outcomes.append(record["pid"])

        threads = [threading.Thread(target=worker, args=(pid,)) for pid in (PID, OTHER_PID)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(outcomes), 2)
        claimed = [value for value in outcomes if value in (PID, OTHER_PID)]
        self.assertEqual(len(claimed), 1)
        # The loser sees either the winner's claim or the non-blocking lock.
        loser = [value for value in outcomes if value not in (PID, OTHER_PID)]
        self.assertEqual(len(loser), 1)
        self.assertIn(loser[0], (guard.ALREADY_DECIDED, guard.BUSY))
        self.assertEqual(self.stored()["pid"], claimed[0])
        self.assertEqual(self.stored()["phase"], "pending")

    def test_concurrent_consume_claims_once_across_processes(self):
        self.arm()
        self.start_process(OTHER_PID, OTHER_START)
        commands = []
        for pid in (PID, OTHER_PID):
            argv = [
                sys.executable, str(self.helper), str(NATIVE), str(self.proc_root),
                str(self.boot_file), "consume", str(self.state), str(self.root), MODE,
                json.dumps(self.environment()), str(pid),
            ]
            commands.append(subprocess.Popen(argv, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True))
        results = [process.communicate(timeout=120) + (process.returncode,)
                   for process in commands]
        payloads = [json.loads(stdout) for stdout, _, _ in results]
        codes = sorted(returncode for _, _, returncode in results)
        self.assertEqual(codes, [0, 3])
        losers = [payload for payload in payloads if "error" in payload]
        self.assertEqual(len(losers), 1)
        self.assertIn(losers[0]["error"], (guard.ALREADY_DECIDED, guard.BUSY))
        winners = [payload for payload in payloads if "error" not in payload]
        self.assertEqual(winners[0]["phase"], "pending")
        self.assertEqual(self.stored()["pid"], winners[0]["pid"])

    def test_missing_or_invalid_invocation_never_claims(self):
        self.arm()
        before = self.stored_bytes()
        self.assertRefused(guard.INVOCATION_INVALID, self.consume, environ={})
        self.assertRefused(guard.INVOCATION_INVALID, self.consume,
                           environ={"INVOCATION_ID": INVOCATION.upper()})
        self.assertRefused(guard.INVOCATION_INVALID, self.consume,
                           environ={"INVOCATION_ID": "a" * 31})
        self.assertEqual(self.stored_bytes(), before)

    def test_missing_armed_record_never_launches(self):
        self.assertRefused(guard.DIRECTORY_MISSING, self.consume)
        self.assertFalse(self.state_dir.exists())
        self.arm()
        self.consume()
        self.assertRefused(guard.ALREADY_DECIDED, self.consume,
                           environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))

    def test_identity_mismatch_between_runtime_and_state_is_refused(self):
        other_root = self.base / "50.1.socket-stream.fedcba9876543210"
        other_root.mkdir()
        (other_root / "gnome-shell").write_bytes(b"#!/bin/sh\n")
        self.arm()
        before = self.stored_bytes()
        self.assertRefused(
            guard.IDENTITY_MISMATCH, guard.consume, self.state, other_root, MODE,
            environ=self.environment(), pid=PID, proc=self.proc)
        self.assertRefused(
            guard.IDENTITY_MISMATCH, guard.arm, self.state, self.root, "wayland",
            proc=self.proc)
        self.assertEqual(self.stored_bytes(), before)


class FailureProtocolTests(GuardFixture):
    def test_power_loss_claim_blocks_launch_until_a_fresh_arm(self):
        self.arm()
        helper_record = self.helper_action("consume", environ=self.environment(), pid=PID)
        self.assertEqual(helper_record["phase"], "pending")

        # Power loss: the process is gone and the next boot has a new identifier.
        self.kill_process(PID)
        self.boot_file.write_text(OTHER_BOOT + "\n", encoding="ascii")
        self.start_process(OTHER_PID, OTHER_START, exe=self.shell)

        self.assertRefused(guard.ALREADY_DECIDED, self.consume, pid=OTHER_PID)
        self.assertRefused(guard.BOOT_CHANGED, self.ready)
        self.assertEqual(self.stored()["phase"], "pending")

        # Only an explicit arm after a new passing probe recovers the session.
        self.assertEqual(self.arm()["phase"], "armed")
        recovered = self.helper_action(
            "consume", environ=self.environment(INVOCATION_ID=OTHER_INVOCATION), pid=OTHER_PID)
        self.assertEqual(recovered["phase"], "pending")
        self.assertEqual(recovered["boot_id"], OTHER_BOOT)

    def test_stale_ready_record_recovers_only_through_a_fresh_arm(self):
        self.arm()
        self.consume()
        self.ready()
        self.kill_process(PID)
        self.boot_file.write_text(OTHER_BOOT + "\n", encoding="ascii")
        self.start_process(OTHER_PID, OTHER_START)
        self.assertRefused(guard.ALREADY_DECIDED, self.consume, pid=OTHER_PID)
        self.assertRefused(guard.NOT_PENDING, self.ready)
        self.assertEqual(self.stored()["phase"], "ready")
        self.assertEqual(self.arm()["phase"], "armed")

    def test_pre_ready_failure_blocks_without_rearm(self):
        self.arm()
        self.consume()
        blocked = self.finish(SERVICE_RESULT="exit-code", EXIT_CODE="exited", EXIT_STATUS="1")
        self.assertEqual(blocked["phase"], "blocked")
        self.assertEqual(blocked["reason"], "pending-failed")
        self.assertRefused(guard.BLOCKED, self.consume,
                           environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))
        self.assertRefused(guard.NOT_PENDING, self.ready)
        # A later stock or stale hook can never re-arm it.
        self.assertRefused(guard.PHASE_REFUSED, self.finish,
                           SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(self.stored()["phase"], "blocked")

    def test_post_ready_crash_blocks_with_identity_retained(self):
        self.arm()
        self.consume()
        self.ready()
        blocked = self.finish(SERVICE_RESULT="signal", EXIT_CODE="killed", EXIT_STATUS="11")
        self.assertEqual(blocked["phase"], "blocked")
        self.assertEqual(blocked["reason"], "unclean-stop")
        self.assertEqual(blocked["invocation_id"], INVOCATION)
        self.assertEqual(self.stored(), blocked)

    def test_timeout_and_missing_result_are_not_rearmed(self):
        for extra in ({"SERVICE_RESULT": "timeout"},
                      {"SERVICE_RESULT": "oom-kill"},
                      {}):
            with self.subTest(extra=extra):
                self.arm()
                self.consume()
                self.ready()
                record = self.finish(**extra)
                self.assertEqual(record["phase"], "blocked")
                self.assertRefused(guard.BLOCKED, self.consume,
                                   environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))

    def test_exit_one_is_not_rearmed_despite_upstream_success_exit_status(self):
        self.arm()
        self.consume()
        self.ready()
        record = self.finish(SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="1")
        self.assertEqual(record["phase"], "blocked")
        self.assertEqual(record["reason"], "unclean-stop")

    def test_clean_stop_zero_rearms_and_consumes_again(self):
        self.arm()
        self.consume()
        self.ready()
        record = self.finish(SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(record["phase"], "armed")
        self.assertEqual(self.stored()["phase"], "armed")
        self.assertEqual(self.consume(environ=self.environment(
            INVOCATION_ID=OTHER_INVOCATION))["phase"], "pending")

    def test_mismatched_old_hooks_never_overwrite_a_valid_record(self):
        self.arm()
        self.consume()
        before = self.stored_bytes()
        self.assertRefused(guard.INVOCATION_MISMATCH, self.ready,
                           environ=self.ready_environment(INVOCATION_ID=OTHER_INVOCATION))
        self.assertRefused(guard.INVOCATION_MISMATCH, self.finish,
                           INVOCATION_ID=OTHER_INVOCATION, SERVICE_RESULT="success",
                           EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(self.stored_bytes(), before)

        self.ready()
        before = self.stored_bytes()
        self.assertRefused(guard.INVOCATION_MISMATCH, self.finish,
                           INVOCATION_ID=OTHER_INVOCATION, SERVICE_RESULT="success",
                           EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.stored()["phase"], "ready")

    def test_finish_without_a_record_never_creates_or_rearms(self):
        self.assertRefused(guard.DIRECTORY_MISSING, self.finish,
                           SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertFalse(self.state_dir.exists())
        self.arm()
        before = self.stored_bytes()
        self.assertRefused(guard.INVOCATION_MISMATCH, self.finish,
                           SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(self.stored_bytes(), before)

    def test_stock_ready_is_refused_and_changes_nothing(self):
        self.arm()
        before = self.stored_bytes()
        self.assertRefused(guard.NOT_PENDING, self.ready)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.stored()["phase"], "armed")

    def test_ready_requires_the_recorded_main_process(self):
        self.arm()
        self.consume()
        self.start_process(OTHER_PID, OTHER_START)
        self.assertRefused(guard.MAIN_PID_MISMATCH, self.ready,
                           environ=self.ready_environment(MAINPID=str(OTHER_PID)))
        # The opt-in is absent from the compositor's own environment.
        self.start_process(PID, START, environ={"INVOCATION_ID": INVOCATION})
        self.assertRefused(guard.EXCLUSION_MISSING, self.ready)
        self.start_process(PID, START, environ={
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "0", "INVOCATION_ID": INVOCATION})
        self.assertRefused(guard.EXCLUSION_MISSING, self.ready)
        self.assertRefused(guard.NOT_CUSTOM_SHELL, self.ready,
                           proc=self._stock_proc())
        self.assertRefused(guard.PROCESS_UNKNOWN, self.ready, proc=self._dead_proc())
        self.assertEqual(self.stored()["phase"], "pending")

    def test_opt_in_comes_from_the_main_process_not_the_helper(self):
        self.arm()
        self.consume()
        # Realistic split: the helper's environment never has the opt-in, the
        # compositor's own environment does.
        self.assertNotIn("SOCKET_STREAM_CAPTURE_EXCLUDE", self.ready_environment())
        record = self.ready()
        self.assertEqual(record["phase"], "ready")

    def test_helper_environment_cannot_spoof_the_opt_in(self):
        self.arm()
        self.consume()
        self.start_process(PID, START, environ={"INVOCATION_ID": INVOCATION})
        before = self.stored_bytes()
        self.assertRefused(guard.EXCLUSION_MISSING, self.ready,
                           environ=self.ready_environment(SOCKET_STREAM_CAPTURE_EXCLUDE="1"))
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.stored()["phase"], "pending")

    def test_main_environment_must_belong_to_the_same_invocation(self):
        self.arm()
        self.consume()
        self.start_process(PID, START, environ={
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "1", "INVOCATION_ID": OTHER_INVOCATION})
        self.assertRefused(guard.INVOCATION_MISMATCH, self.ready)

    def test_main_environment_is_bounded_and_whitelisted(self):
        self.arm()
        self.consume()
        directory = self.proc_root / str(PID)
        (directory / "environ").write_bytes(
            b"SECRET_TOKEN=leak\0SOCKET_STREAM_CAPTURE_EXCLUDE=1\0")
        exported = self.proc.environment(PID)
        self.assertEqual(exported, {"SOCKET_STREAM_CAPTURE_EXCLUDE": "1"})
        self.assertEqual(self.ready()["phase"], "ready")
        # Oversized blocks are refused rather than partially parsed.
        (directory / "environ").write_bytes(
            b"SOCKET_STREAM_CAPTURE_EXCLUDE=1\0" + b"X" * (guard.MAX_ENVIRON + 1024))
        self.assertRefused(guard.ENVIRONMENT_INVALID, self.proc.environment, PID)

    def test_foreign_main_process_owner_is_refused(self):
        self.arm()
        self.consume()
        foreign = _ForeignOwnerProc(root=self.proc_root, boot_id_path=self.boot_file)
        self.assertRefused(guard.PROCESS_FOREIGN, self.ready, proc=foreign)
        self.assertRefused(guard.PROCESS_UNKNOWN, self.ready,
                           proc=guard.Proc(root=self.base / "empty-proc",
                                           boot_id_path=self.boot_file))
        self.assertEqual(self.stored()["phase"], "pending")

    def _stock_proc(self):
        stock = self.base / "stock-gnome-shell"
        stock.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.start_process(PID, START, exe=stock)
        return self.proc

    def _dead_proc(self):
        dead = guard.Proc(root=self.base / "elsewhere", boot_id_path=self.boot_file)
        return dead

    def test_ready_refuses_stale_boot_and_missing_boot_id(self):
        self.arm()
        self.consume()
        self.boot_file.write_text(OTHER_BOOT + "\n", encoding="ascii")
        self.assertRefused(guard.BOOT_CHANGED, self.ready)
        self.boot_file.write_text("not-a-boot-id\n", encoding="ascii")
        self.assertRefused(guard.BOOT_CHANGED, self.ready)
        self.assertEqual(self.stored()["phase"], "pending")

    def test_disarm_leaves_a_blocking_marker(self):
        self.arm()
        self.consume()
        self.ready()
        record = guard.disarm(self.state, self.root, MODE, proc=self.proc)
        self.assertEqual(record["phase"], "blocked")
        self.assertEqual(record["reason"], "disarmed")
        for key in guard.IDENTITY_KEYS:
            self.assertNotIn(key, record)
        self.assertRefused(guard.BLOCKED, self.consume,
                           environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))
        self.assertRefused(guard.NOT_PENDING, self.ready)
        self.assertRefused(guard.INVOCATION_MISMATCH, self.finish,
                           SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(self.stored()["phase"], "blocked")
        # An explicit activation after removal may arm again, but only that.
        self.assertEqual(self.arm()["phase"], "armed")


class DurabilityTests(GuardFixture):
    def test_file_sync_failure_refuses_the_launch_and_keeps_armed(self):
        self.arm()
        before = self.stored_bytes()
        with patch.object(guard, "_sync_file", side_effect=OSError(5, "synthetic")):
            self.assertRefused(guard.WRITE_FAILED, self.consume)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.stored()["phase"], "armed")
        self.assertEqual(sorted(path.name for path in self.state_dir.iterdir()),
                         sorted([self.state.name, guard.lock_name(self.state)]))

    def test_directory_sync_failure_refuses_the_launch(self):
        self.arm()
        with patch.object(guard, "_sync_directory", side_effect=OSError(5, "synthetic")):
            self.assertRefused(guard.WRITE_FAILED, self.consume)
        self.assertEqual(self.stored()["phase"], "armed")
        self.assertEqual(self.consume()["phase"], "pending")

    def test_write_failure_refuses_the_launch_and_keeps_armed(self):
        self.arm()
        before = self.stored_bytes()
        with patch.object(guard, "_write_atomic",
                          side_effect=guard.GuardError(guard.WRITE_FAILED)):
            self.assertRefused(guard.WRITE_FAILED, self.consume)
        self.assertEqual(self.stored_bytes(), before)

    def test_partial_manifest_never_replaces_the_previous_record(self):
        self.arm()
        before = self.stored_bytes()
        with patch.object(guard.os, "replace", side_effect=OSError(5, "synthetic")):
            self.assertRefused(guard.WRITE_FAILED, self.consume)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.state_dir.iterdir()),
                         sorted([self.state.name, guard.lock_name(self.state)]))

    def test_commit_collapses_record_to_bounded_json(self):
        self.arm()
        self.consume()
        data = self.stored_bytes()
        self.assertLessEqual(len(data), guard.MAX_RECORD)
        self.assertNotIn(b"\n\n", data)
        self.assertEqual(data.decode("ascii").count("\n"), 1)
        self.assertEqual(set(self.stored()),
                         {"schema", "runtime", "mode", "phase",
                          "invocation_id", "boot_id", "pid", "starttime"})
        with self.assertRaises(guard.GuardError):
            guard._encode({"schema": 1, "runtime": "/x/" + "a" * 9000, "mode": MODE,
                           "phase": "armed"})


class LockTests(GuardFixture):
    def hold_lock(self):
        """Hold the guard's own lock, exactly as a concurrent action would."""
        self.arm()
        descriptor = os.open(self.state_dir / guard.lock_name(self.state), os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_a_held_lock_refuses_immediately_instead_of_waiting(self):
        self.hold_lock()
        # The CLI runs in a child with a timeout, so an implementation that
        # waited on the lock would fail here instead of hanging the suite.
        result = self.cli("consume", environ={"INVOCATION_ID": INVOCATION})
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.BUSY, result.stderr)
        result = self.cli("arm")
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.BUSY, result.stderr)
        refused = self.helper_process("finish", environ=self.environment(
            SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0"))
        self.assertEqual(refused.returncode, 3)
        self.assertEqual(json.loads(refused.stdout), {"error": guard.BUSY})
        self.assertEqual(self.stored()["phase"], "armed")

    def test_status_reports_a_locked_state_without_taking_the_lock(self):
        self.hold_lock()
        self.assertEqual(self.status()["phase"], "armed")
        self.assertEqual(self.cli("status").returncode, 0)

    def test_the_lock_is_reusable_after_release(self):
        descriptor = self.hold_lock()
        self.assertRefused(guard.BUSY, self.consume)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertEqual(self.consume()["phase"], "pending")


class HostilePathTests(GuardFixture):
    def test_state_symlink_is_never_followed(self):
        outside = self.base / "outside.json"
        outside.write_bytes(b"do not touch\n")
        self.state_dir.mkdir(mode=0o700)
        self.state.symlink_to(outside)
        self.assertRefused(guard.STATE_UNSAFE, self.arm)
        self.assertEqual(outside.read_bytes(), b"do not touch\n")
        self.assertRefused(guard.STATE_UNSAFE, self.consume)
        self.assertEqual(outside.read_bytes(), b"do not touch\n")

    def test_replacing_state_with_a_symlink_cannot_redirect_writes(self):
        self.arm()
        outside = self.base / "outside.json"
        outside.write_bytes(b"keep\n")
        self.state.unlink()
        self.state.symlink_to(outside)
        # The record is unreadable as a symlink, so every action refuses first.
        self.assertRefused(guard.STATE_UNSAFE, self.arm)
        self.assertEqual(outside.read_bytes(), b"keep\n")
        # Even the raw write primitive replaces the link, never its target.
        descriptor = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            guard._write_atomic(descriptor, self.state, b'{"phase":"armed"}\n')
        finally:
            os.close(descriptor)
        self.assertFalse(self.state.is_symlink())
        self.assertEqual(outside.read_bytes(), b"keep\n")

    def test_state_directory_symlink_is_refused(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        marker = elsewhere / "keep.txt"
        marker.write_text("keep", encoding="ascii")
        self.state_dir.symlink_to(elsewhere)
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.arm)
        self.assertEqual(marker.read_text(encoding="ascii"), "keep")
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in elsewhere.iterdir()), ["keep.txt"])

    def test_directory_collision_never_chmods_an_existing_entry(self):
        # An unrelated directory already sits at the dedicated name.
        self.state_dir.mkdir()
        os.chmod(self.state_dir, 0o755)
        marker = self.state_dir / "keep.txt"
        marker.write_text("keep", encoding="ascii")
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.arm)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o755)
        self.assertEqual(marker.read_text(encoding="ascii"), "keep")

    def test_symlink_racing_the_mkdir_collision_is_refused_and_untouched(self):
        elsewhere = self.base / "raced-target"
        elsewhere.mkdir(mode=0o700)
        marker = elsewhere / "keep.txt"
        marker.write_text("keep", encoding="ascii")
        original = guard.os.open

        def racing_open(name, flags, *args, **kwargs):
            if name == "mutter-startup" and not getattr(racing_open, "raced", False):
                # The component vanishes between the open and the mkdir.
                racing_open.raced = True
                self.state_dir.symlink_to(elsewhere)
                raise FileNotFoundError(2, "raced")
            return original(name, flags, *args, **kwargs)

        with patch.object(guard.os, "open", racing_open):
            self.assertRefused(guard.DIRECTORY_UNSAFE, self.arm)
        self.assertEqual(marker.read_text(encoding="ascii"), "keep")
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in elsewhere.iterdir()), ["keep.txt"])

    def test_writes_stay_in_the_validated_directory_after_a_swap(self):
        self.arm()
        descriptor = guard._open_state_directory(self.state, self.uid, create=False)
        self.addCleanup(os.close, descriptor)
        moved = self.base / "moved-aside"
        os.rename(self.state_dir, moved)
        elsewhere = self.base / "swapped-in"
        elsewhere.mkdir(mode=0o700)
        self.state_dir.symlink_to(elsewhere)
        # The path is now refused, but the pinned descriptor is still the
        # directory that was validated: nothing lands in the swap target.
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.consume)
        guard._write_atomic(descriptor, self.state, b'{"phase":"armed"}\n')
        self.assertTrue((moved / self.state.name).is_file())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_untrusted_state_directory_modes_are_refused(self):
        self.state_dir.mkdir()
        os.chmod(self.state_dir, 0o755)
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.arm)
        os.chmod(self.state_dir, 0o700)
        self.assertEqual(self.arm()["phase"], "armed")
        os.chmod(self.state_dir, 0o750)
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.consume)

    def test_writable_ancestor_is_refused(self):
        self.arm()
        parent = self.state_dir.parent
        os.chmod(parent, 0o777)
        self.addCleanup(os.chmod, parent, 0o755)
        self.assertRefused(guard.DIRECTORY_UNSAFE, self.consume)
        self.assertEqual(self.stored()["phase"], "armed")

    def test_state_and_lock_permissions_are_enforced(self):
        self.arm()
        os.chmod(self.state, 0o644)
        self.assertRefused(guard.STATE_UNSAFE, self.consume)
        self.assertRefused(guard.STATE_UNSAFE, self.status)
        os.chmod(self.state, 0o600)
        lock = self.state_dir / guard.lock_name(self.state)
        os.chmod(lock, 0o666)
        self.assertRefused(guard.LOCK_UNSAFE, self.consume)
        # The lock is never unlinked, so the refusal is stable.
        self.assertTrue(lock.is_file())

    def test_status_never_writes_or_creates_the_lock(self):
        self.assertEqual(self.status()["phase"], "missing")
        self.assertFalse(self.state_dir.exists())
        self.state_dir.mkdir(mode=0o700)
        record = {"schema": 1, "runtime": str(self.root), "mode": MODE, "phase": "armed"}
        self.state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
        os.chmod(self.state, 0o600)
        before = self.state.read_bytes()
        for _ in range(3):
            report = self.status()
            self.assertEqual(report["phase"], "armed")
            self.assertEqual(report["identity"], "same")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual([path.name for path in self.state_dir.iterdir()],
                         [self.state.name])

    def test_status_reports_mismatched_identity_without_writing(self):
        self.arm()
        other_root = self.base / "50.1.socket-stream.fedcba9876543210"
        other_root.mkdir()
        before = self.stored_bytes()
        report = guard.status(self.state, other_root, "wayland", proc=self.proc)
        self.assertEqual(report["phase"], "armed")
        self.assertEqual(report["identity"], "different")
        self.assertEqual(self.stored_bytes(), before)

    def test_only_a_root_owned_sticky_ancestor_may_be_world_writable(self):
        class Info:
            def __init__(self, mode, uid):
                self.st_mode = mode
                self.st_uid = uid

        trusted = guard._trusted_directory
        uid = os.getuid()
        self.assertTrue(trusted(Info(stat.S_IFDIR | 0o1777, 0), uid, ancestor=True))
        self.assertFalse(trusted(Info(stat.S_IFDIR | 0o1777, 0), uid, ancestor=False))
        self.assertFalse(trusted(Info(stat.S_IFDIR | 0o1777, uid), uid, ancestor=True))
        self.assertFalse(trusted(Info(stat.S_IFDIR | 0o0777, 0), uid, ancestor=True))
        self.assertTrue(trusted(Info(stat.S_IFDIR | 0o0755, 0), uid, ancestor=True))
        self.assertTrue(trusted(Info(stat.S_IFDIR | 0o0700, uid), uid, ancestor=False))
        self.assertFalse(trusted(Info(stat.S_IFREG | 0o0700, uid), uid, ancestor=False))
        self.assertFalse(trusted(Info(stat.S_IFDIR | 0o0750, uid + 1), uid, ancestor=False))

    def test_state_path_rejects_parent_components(self):
        for name in ("../x.json", "mutter-startup/../x.json", "a/../../x.json"):
            with self.subTest(name=name):
                self.assertRefused(
                    guard.USAGE_INVALID, guard.arm,
                    self.base / "config/socket_stream" / name, self.root, MODE,
                    proc=self.proc)
        # pathlib already folds "." away, so a dotted path is not a hazard.
        self.assertEqual(Path("/a/./b.json").parts, ("/", "a", "b.json"))

    def test_runtime_root_must_be_canonical_and_trusted(self):
        link = self.base / "50.1.socket-stream.alias0123456789"
        link.symlink_to(self.root)
        self.assertRefused(guard.INVALID_ROOT, guard.arm, self.state, link, MODE,
                           proc=self.proc)
        relative = Path("relative")
        self.assertRefused(guard.INVALID_ROOT, guard.arm, self.state, relative, MODE,
                           proc=self.proc)
        lowercase = self.base / "bad name"
        lowercase.mkdir()
        self.assertRefused(guard.INVALID_ROOT, guard.arm, self.state, lowercase, MODE,
                           proc=self.proc)
        os.chmod(self.root, 0o777)
        self.addCleanup(os.chmod, self.root, 0o755)
        self.assertRefused(guard.INVALID_ROOT, guard.arm, self.state, self.root, MODE,
                           proc=self.proc)

    def test_mode_must_be_plain(self):
        for mode in ("", "a b", "ubuntu@wayland", "x" * 65, "../x"):
            with self.subTest(mode=mode):
                self.assertRefused(guard.USAGE_INVALID, guard.arm, self.state, self.root,
                                   mode, proc=self.proc)

    def test_corrupt_records_are_refused_not_overwritten(self):
        self.state_dir.mkdir(mode=0o700)
        cases = (
            b"",
            b"{}\n",
            b'{"schema":2,"runtime":"%s","mode":"ubuntu","phase":"armed"}\n',
            b'{"schema":1,"phase":"armed"}\n',
            b'{"schema":1,"runtime":"%s","mode":"ubuntu","phase":"armed","extra":1}\n',
            b'{"schema":1,"runtime":"%s","mode":"ubuntu","phase":"pending"}\n',
            b'{"schema":1,"runtime":"%s","mode":"ubuntu","phase":"armed","reason":"x"}\n',
            b'{"schema":1,"runtime":"%s","mode":"ubuntu","phase":"armed","schema":1}\n',
        )
        for case in cases:
            with self.subTest(case=case):
                data = case.replace(b"%s", str(self.root).encode())
                self.state.write_bytes(data)
                os.chmod(self.state, 0o600)
                self.assertRefused(guard.STATE_INVALID, self.arm)
                self.assertEqual(self.state.read_bytes(), data)


class ProcFixtureTests(GuardFixture):
    def test_starttime_is_field_22_with_hostile_comm(self):
        directory = self.proc_root / "777"
        directory.mkdir()
        (directory / "stat").write_text(
            "777 (gnome shell) x) S 1 777 777 0 -1 4194560 100 0 0 0 5 6 0 0 "
            "20 0 12 0 555111 123 456\n", encoding="ascii")
        self.assertEqual(self.proc.starttime(777), 555111)
        self.assertIsNone(self.proc.starttime(778))
        (directory / "stat").write_text("777 (gnome shell) S 1 2 3\n", encoding="ascii")
        self.assertIsNone(self.proc.starttime(777))

    def test_executable_identity_requires_a_regular_target(self):
        self.assertEqual(self.proc.executable(PID), guard._file_identity(self.shell))
        self.assertIsNone(self.proc.executable(OTHER_PID))
        directory = self.proc_root / "999"
        directory.mkdir()
        (directory / "exe").symlink_to(self.proc_root)
        self.assertIsNone(self.proc.executable(999))

    def test_boot_identifier_is_normalised_and_validated(self):
        self.boot_file.write_text(BOOT.upper() + "\n", encoding="ascii")
        self.assertEqual(self.proc.boot(), BOOT)
        self.boot_file.write_text("", encoding="ascii")
        self.assertRefused(guard.BOOT_CHANGED, self.proc.boot)


class MutterMappingTests(GuardFixture):
    """ready() must prove the private Mutter family, not just exe/env/boot."""

    def pending(self):
        self.arm()
        self.consume()

    def test_ready_refuses_an_extra_stock_mutter_mapping(self):
        self.pending()
        stock = "/usr/lib/x86_64-linux-gnu/" + LIBRARIES[0].split("/")[-1]
        extra = self.mapping_line(self.root / LIBRARIES[0], pathname=stock)
        self.write_maps(lines=self.default_maps() + [extra])
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)
        self.assertEqual(self.stored()["phase"], "pending")

    def test_ready_refuses_a_foreign_family_name(self):
        self.pending()
        foreign = "7f0000000000-7f0000001000 r-xp 00000000 08:01 999 " \
                  "/usr/lib/x86_64-linux-gnu/libmutter-19.so.0\n"
        self.write_maps(lines=self.default_maps() + [foreign])
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_a_missing_family_mapping(self):
        self.pending()
        self.write_maps(lines=self.default_maps()[:-1])
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_a_missing_family_file(self):
        self.pending()
        (self.root / LIBRARIES[0]).unlink()
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_a_different_inode(self):
        self.pending()
        lines = self.default_maps()
        lines[0] = self.mapping_line(self.root / LIBRARIES[0], inode=1)
        self.write_maps(lines=lines)
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_a_deleted_mapping(self):
        self.pending()
        lines = self.default_maps()
        lines[0] = self.mapping_line(self.root / LIBRARIES[0], deleted=True)
        self.write_maps(lines=lines)
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_oversized_maps(self):
        self.pending()
        with (self.proc_root / str(PID) / "maps").open("wb") as stream:
            stream.truncate(guard.MAX_MAPS + 1)
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_ready_refuses_unreadable_maps(self):
        self.pending()
        directory = self.proc_root / str(PID)
        (directory / "maps").unlink()
        (directory / "maps").symlink_to(self.base / "absent-maps")
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)

    def test_mapping_refusal_then_clean_stop_blocks_and_never_rearms(self):
        self.pending()
        self.write_maps(lines=self.default_maps()[:-1])
        self.assertRefused(guard.MAPPINGS_INVALID, self.ready)
        self.assertEqual(self.stored()["phase"], "pending")
        # A clean stop after a refused promotion must block, not re-arm.
        blocked = self.finish(
            SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        self.assertEqual(blocked["phase"], "blocked")
        self.assertEqual(blocked["reason"], "pending-failed")
        self.assertRefused(guard.BLOCKED, self.consume,
                           environ=self.environment(INVOCATION_ID=OTHER_INVOCATION))
        self.assertRefused(guard.NOT_PENDING, self.ready)

    def test_matching_private_mappings_still_promote(self):
        self.pending()
        self.assertEqual(self.ready()["phase"], "ready")


class RestrictiveUmaskTests(GuardFixture):
    def test_restrictive_umask_still_completes_the_cycle(self):
        # A fresh tree so every component below is created by the guard.
        plain = self.base / "umask-home"
        plain.mkdir()
        state = plain / "socket_stream/mutter-startup" / ("f" * 32 + ".json")
        script = self.base / "umask_helper.py"
        script.write_text(UMASK_HELPER, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), str(NATIVE), str(self.proc_root),
             str(self.boot_file), str(state), str(self.root), MODE,
             INVOCATION, str(PID)],
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "ready")
        self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        lock = state.parent / guard.lock_name(state)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


class InteropTests(GuardFixture):
    def test_arm_creates_the_pinned_private_state_tree(self):
        plain = self.base / "fresh-home"
        plain.mkdir()
        state = plain / "socket_stream/mutter-startup" / ("f" * 32 + ".json")
        record = guard.arm(state, self.root, MODE, proc=self.proc)
        self.assertEqual(record["phase"], "armed")
        self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)

    def test_app_written_blocked_marker_is_accepted_and_recoverable(self):
        # The app's fallback writes this exact minimal record when the runtime,
        # and therefore this helper, is already gone.
        self.state.parent.mkdir(parents=True)
        os.chmod(self.state.parent, 0o700)
        self.state.write_text(json.dumps(
            {"schema": 1, "runtime": str(self.root), "mode": MODE, "phase": "blocked"}),
            encoding="ascii")
        os.chmod(self.state, 0o600)
        self.assertEqual(self.status()["phase"], "blocked")
        self.assertRefused(guard.BLOCKED, self.consume)
        self.assertEqual(self.arm()["phase"], "armed")
        self.assertEqual(self.stored()["phase"], "armed")

    def test_guard_never_spawns_supervises_or_executes(self):
        source = (NATIVE / "startup_guard.py").read_text(encoding="utf-8")
        for forbidden in ("import subprocess", "os.fork", "os.exec", "Popen",
                          "setsid", "threading", "import socket"):
            self.assertNotIn(forbidden, source)


class CommandLineTests(GuardFixture):
    def test_cli_arm_status_disarm(self):
        result = self.cli("arm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "armed")
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "armed")
        result = self.cli("disarm")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["phase"], payload["reason"]), ("blocked", "disarmed"))

    def test_cli_reports_missing_status_without_writing(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "missing")
        self.assertFalse(self.state_dir.exists())

    def test_cli_refusals_use_fixed_errors(self):
        result = self.cli("ready", environ={
            "INVOCATION_ID": INVOCATION, "MAINPID": str(PID),
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(guard.DIRECTORY_MISSING, result.stderr)
        self.assertTrue(result.stderr.startswith("SocketStream guard: "))

        self.cli("arm")
        result = self.cli("ready", environ={
            "INVOCATION_ID": INVOCATION, "MAINPID": str(PID),
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.NOT_PENDING, result.stderr)
        self.assertEqual(self.stored()["phase"], "armed")

        result = self.cli("finish", environ={})
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.INVOCATION_INVALID, result.stderr)
        self.assertEqual(self.stored()["phase"], "armed")

        result = self.cli("consume", environ={})
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.INVOCATION_INVALID, result.stderr)

    def test_cli_refuses_unbounded_arguments(self):
        result = self.cli("arm", state=Path("relative.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.USAGE_INVALID, result.stderr)
        result = self.cli("arm", mode="ubuntu@wayland")
        self.assertEqual(result.returncode, 1)
        self.assertIn(guard.USAGE_INVALID, result.stderr)
        result = self.cli("nonsense")
        self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(os.path.isdir("/proc"), "requires a live /proc")
    def test_cli_consume_claims_once_with_live_proc(self):
        live = guard.Proc(root=Path("/proc"), boot_id_path=Path("/proc/sys/kernel/random/boot_id"))
        guard.arm(self.state, self.root, MODE, proc=live)
        first = self.cli("consume", environ={"INVOCATION_ID": INVOCATION})
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["phase"], "pending")
        self.assertEqual(payload["invocation_id"], INVOCATION)
        self.assertGreater(payload["starttime"], 0)
        second = self.cli("consume", environ={"INVOCATION_ID": OTHER_INVOCATION})
        self.assertEqual(second.returncode, 1)
        self.assertIn(guard.ALREADY_DECIDED, second.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout)["phase"], "pending")


if __name__ == "__main__":
    unittest.main()
