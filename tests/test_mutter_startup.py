"""Credential, mixed-GI regression, bounded isolation and fail-closed probe checks."""
import importlib.util
from contextlib import ExitStack, suppress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

FILE = Path(__file__).resolve().parents[1] / "native/mutter-capture-filter/startup_probe.py"
SPEC = importlib.util.spec_from_file_location("mutter_startup_probe_tests", FILE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class StartupFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "runtime"
        (self.root / "lib").mkdir(parents=True)
        self.shell = self.root / "gnome-shell"
        self.shell.write_bytes(b"private executable fixture")
        self.pid, self.pgid, self.uid = 501, 500, os.getuid()
        self.proc = self.base / "proc"
        self.process = self.proc / str(self.pid)
        self.process.mkdir(parents=True)
        # /proc stat field22 is tail[19], even if the process name contains ')'.
        self.process.joinpath("stat").write_text(
            f"{self.pid} (gnome-shell name)) S " + "0 " * 18 + "4321 0\n")
        self.process.joinpath("exe").symlink_to(self.shell)
        self.lines = []
        for name in probe.LIBRARIES:
            file = self.root / name
            file.write_bytes(name.encode())
            info = file.stat()
            self.lines.append(f"1000-2000 r-xp 0000 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x} {info.st_ino} {file}")
        self.write_maps()

    def write_maps(self):
        self.process.joinpath("maps").write_text("\n".join(self.lines) + "\n")

    def sender(self, *, data=b"READY=1\n", sender_uid=None, pgid=None, flags=0):
        with patch.object(socket, "SCM_CREDENTIALS", 2, create=True):
            return probe.authenticated_sender(
                data, [(socket.SOL_SOCKET, 2, struct.pack("3i", self.pid,
                        self.uid if sender_uid is None else sender_uid, 1000))], flags,
                shell=self.shell, pgid=self.pgid, uid=self.uid, proc=self.proc,
                getpgid=lambda pid: self.pgid if pgid is None else pgid,
            )


class StartupEvidence(StartupFixture):
    def test_genuine_shell_sender_has_process_start_identity(self):
        self.assertEqual(self.sender(), (self.pid, 4321))

    def test_dbus_daemon_ready_is_not_shell_ready(self):
        daemon = self.base / "dbus-daemon"
        daemon.write_bytes(b"daemon fixture")
        self.process.joinpath("exe").unlink()
        self.process.joinpath("exe").symlink_to(daemon)
        self.assertIsNone(self.sender())

    def test_wrong_uid_group_truncation_or_nonexact_ready_cannot_pass(self):
        for arguments in ({"sender_uid": self.uid + 1}, {"pgid": 999},
                          {"flags": socket.MSG_TRUNC}, {"flags": socket.MSG_CTRUNC},
                          {"data": b"STATUS=READY=1"}, {"data": b"READY=10"}):
            with self.subTest(arguments=arguments):
                self.assertIsNone(self.sender(**arguments))

    def test_all_segments_of_the_private_family_are_required(self):
        self.lines.append(self.lines[0])
        self.write_maps()
        self.assertEqual(probe.mapped_libraries(self.root, self.pid, proc=self.proc),
                         sorted(str(self.root / name) for name in probe.LIBRARIES))
        self.lines = self.lines[:3]
        self.write_maps()
        with self.assertRaisesRegex(probe.ProbeError, "all four"):
            probe.mapped_libraries(self.root, self.pid, proc=self.proc)

    def test_stock_gi_dlopen_beside_custom_library_is_detected(self):
        self.lines.append(self.lines[0].replace(str(self.root / "lib"), "/usr/lib"))
        self.write_maps()
        with self.assertRaisesRegex(probe.ProbeError, "mixed stock and custom"):
            probe.mapped_libraries(self.root, self.pid, proc=self.proc)

    def test_deleted_mapping_or_different_inode_cannot_count(self):
        original = list(self.lines)
        self.lines[0] += " (deleted)"
        self.write_maps()
        with self.assertRaisesRegex(probe.ProbeError, "deleted"):
            probe.mapped_libraries(self.root, self.pid, proc=self.proc)
        self.lines = original
        fields = self.lines[0].split(None, 5)
        fields[4] = str(int(fields[4]) + 1)
        self.lines[0] = " ".join(fields)
        self.write_maps()
        with self.assertRaisesRegex(probe.ProbeError, "mixed stock and custom"):
            probe.mapped_libraries(self.root, self.pid, proc=self.proc)

    def test_fatal_duplicate_type_stays_latched_after_log_tail_rolls(self):
        log = probe.Log()
        log.append(b"cannot register existing ")
        log.append(b"type 'ClutterActor'\n")
        log.append(b"x" * (probe.MAX_LOG_BYTES + 1))
        self.assertTrue(log.fatal)
        self.assertEqual(len(log.tail), probe.MAX_LOG_BYTES)


class HeadlessWorker(StartupFixture):
    """Drive the real worker loop and evidence checks with controlled OS events.

    Actual /proc-shaped files, inodes, credentials and tempfile removal are
    checked. Only Linux process creation, pidfds and event delivery are supplied
    by fixtures so failure paths can run deterministically on macOS as well.
    """

    def worker(self, *, event="ready", cleanup=True, shutdown=0, cleanup_error=False,
               sandbox_error=False, spawn_error=False):
        clock = [0.0]
        descriptors = []
        def descriptor():
            reader, writer = os.pipe()
            os.close(writer)
            descriptors.append(reader)
            return reader
        def close_descriptors():
            for fd in descriptors:
                with suppress(OSError):
                    os.close(fd)
        self.addCleanup(close_descriptors)
        process = SimpleNamespace(pid=self.pgid, returncode=None, stdout=os.fdopen(descriptor(), "rb"))
        self.addCleanup(process.stdout.close)
        process.poll = lambda: process.returncode
        commands, cleanups = [], []
        def popen(argv, **kwargs):
            commands.append((argv, kwargs))
            if spawn_error:
                raise OSError("fixture spawn failure")
            sandbox = Path(kwargs["env"]["HOME"]).parent
            self.assertNotIn("servicedir", (sandbox / "bus.conf").read_text())
            self.assertEqual(kwargs["env"]["GIO_USE_VFS"], "local")
            return process
        def drain(selector, log, timeout=0.1):
            if timeout == 0:
                return []
            clock[0] += 1
            if clock[0] == 1 and event != "silent":
                sender_uid = self.uid + 1 if event == "foreign-ready" else self.uid
                return [(b"READY=1\n", [(socket.SOL_SOCKET, 2,
                         struct.pack("3i", self.pid, sender_uid, 1000))], 0, None)]
            if clock[0] == 2 and event == "fatal":
                log.append(b"cannot register existing type ClutterActor\n")
            if clock[0] == 4 and event == "late-mixed":
                self.lines.append(self.lines[0].replace(str(self.root / "lib"), "/usr/lib"))
                self.write_maps()
            if clock[0] == 4 and event == "pid-reused":
                path = self.process / "stat"
                path.write_text(path.read_text().replace("4321", "4322"))
            return []
        def cleanup_process(child, shell_fd, cgroup, selector, log):
            cleanups.append(child)
            child.returncode = shutdown
            if event == "shutdown-fatal":
                log.append(b"Execution of main.js threw exception\n")
            if cleanup_error:
                raise OSError("fixture cgroup read failure")
            return cleanup
        sender = probe.authenticated_sender
        mappings = probe.mapped_libraries
        identity = probe.process_identity
        remove_sandbox = tempfile.TemporaryDirectory.cleanup
        def remove(directory):
            remove_sandbox(directory)
            if sandbox_error:
                raise OSError("fixture sandbox cleanup failure")
        output = self.base / "evidence"
        with ExitStack() as stack:
            for target, name, value in (
                    (probe.sys, "platform", "linux"), (probe.os, "geteuid", lambda: self.uid or 1000),
                    (probe, "worker_cgroup", lambda: self.base / "cgroup"),
                    (probe, "_runtime_directory", lambda: self.base),
                    (probe.socket, "socket", MagicMock()),
                    (probe.selectors, "DefaultSelector", MagicMock()),
                    (probe.subprocess, "Popen", popen), (probe, "_drain", drain),
                    (probe.time, "monotonic", lambda: clock[0]), (probe, "_cleanup", cleanup_process),
                    (probe.select, "select", lambda *args: ([args[0][0]] if event == "crash" and clock[0] >= 2 else [], [], [])),
                    (probe, "authenticated_sender", lambda *args, **kwargs: sender(
                        *args, **kwargs, proc=self.proc, getpgid=lambda pid: self.pgid)),
                    (probe, "mapped_libraries", lambda root, pid: mappings(root, pid, proc=self.proc)),
                    (probe, "process_identity", lambda pid, **kwargs: identity(pid, proc=self.proc)),
                    (tempfile.TemporaryDirectory, "cleanup", remove)):
                stack.enter_context(patch.object(target, name, value))
            stack.enter_context(patch.object(probe.socket, "SO_PASSCRED", 16, create=True))
            stack.enter_context(patch.object(probe.socket, "SCM_CREDENTIALS", 2, create=True))
            stack.enter_context(patch.object(probe.os, "pidfd_open", lambda pid: descriptor(), create=True))
            result = probe.run_headless(self.root, "ubuntu", output=output, startup_seconds=6)
        self.assertEqual(json.loads((output / "result.json").read_text()), result)
        self.assertEqual(list(self.base.glob("ss-shell-*")), [])
        self.assertEqual(len(cleanups), 0 if spawn_error else 1)
        if not spawn_error:
            self.assertTrue(process.stdout.closed)
        return result, commands

    def test_full_worker_requires_ready_live_private_mappings_and_clean_shutdown(self):
        result, commands = self.worker()
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["shutdown_returncode"], 0)
        self.assertEqual(result["ready_pid"], self.pid)
        self.assertEqual(result["stable_seconds"], 3)
        argv, options = commands[0]
        self.assertIn(str(self.shell), argv)
        self.assertIn('exec /usr/bin/env DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" "$@"', argv)
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["env"]["SOCKET_STREAM_CAPTURE_EXCLUDE"], "1")

    def test_worker_ignores_unauthenticated_ready_until_timeout(self):
        result, _ = self.worker(event="foreign-ready")
        self.assertFalse(result["ok"])
        self.assertFalse(result["ready"])
        self.assertIn("timed out", result["reason"])

    def test_silent_worker_times_out_without_claiming_readiness(self):
        result, _ = self.worker(event="silent")
        self.assertFalse(result["ok"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["cleanup_complete"])

    def test_post_ready_crash_never_passes_the_liveness_window(self):
        result, _ = self.worker(event="crash", shutdown=139)
        self.assertFalse(result["ok"])
        self.assertTrue(result["ready"])
        self.assertIn("exited after announcing", result["reason"])

    def test_late_gi_mapping_is_rechecked_after_liveness(self):
        result, _ = self.worker(event="late-mixed")
        self.assertFalse(result["ok"])
        self.assertIn("mixed stock and custom", result["reason"])

    def test_process_identity_must_survive_the_liveness_window(self):
        result, _ = self.worker(event="pid-reused")
        self.assertFalse(result["ok"])
        self.assertIn("identity changed", result["reason"])

    def test_fatal_shell_log_is_not_overridden_by_ready(self):
        result, _ = self.worker(event="fatal")
        self.assertFalse(result["ok"])
        self.assertIn("fatal JS or duplicate-GType", result["reason"])

    def test_successful_liveness_cannot_hide_failed_cleanup(self):
        result, _ = self.worker(cleanup=False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_complete"])
        self.assertIn("process cleanup", result["reason"])

    def test_unreadable_cleanup_evidence_is_a_failure(self):
        result, _ = self.worker(cleanup_error=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_complete"])

    def test_forced_shutdown_after_stable_readiness_is_a_failure(self):
        result, _ = self.worker(shutdown=137)
        self.assertEqual(result["stable_seconds"], 3)
        self.assertTrue(result["cleanup_complete"])
        self.assertFalse(result["ok"])
        self.assertIn("did not shut down cleanly", result["reason"])

    def test_fatal_shutdown_log_cannot_arrive_after_the_success_decision(self):
        result, _ = self.worker(event="shutdown-fatal")
        self.assertFalse(result["ok"])
        self.assertIn("fatal JS or duplicate-GType", result["reason"])

    def test_disposable_home_removal_is_part_of_cleanup_success(self):
        result, _ = self.worker(sandbox_error=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_complete"])
        self.assertIn("disposable home/runtime", result["reason"])

    def test_spawn_failure_removes_the_sandbox_and_returns_failure(self):
        result, _ = self.worker(spawn_error=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["cleanup_complete"])


class DisposableService(unittest.TestCase):
    root = Path("/opt/socket_stream-mutter/50.1.socket-stream.fixture")

    def valid(self):
        return {"ok": True, "runtime": str(self.root), "mode": "ubuntu", "ready": True,
                "cleanup_complete": True, "shutdown_returncode": 0, "stable_seconds": 3.1, "ready_pid": 111,
                "starttime": 100, "mutter_mappings": sorted(str(self.root / n) for n in probe.LIBRARIES)}

    def test_outer_waits_on_bounded_cgroup_owned_service(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.valid()), "")
        result = probe.run_isolated(self.root, "ubuntu", run=run)
        self.assertTrue(result["ok"])
        argv, kwargs = calls[0]
        for value in ("--user", "--wait", "--collect", "--property=KillMode=control-group",
                      "--property=MemoryMax=1G", "--property=RuntimeMaxSec=50s", "--property=TimeoutStopSec=5s"):
            self.assertIn(value, argv)
        self.assertEqual(kwargs["timeout"], 60)
        self.assertNotIn("--replace", argv)

    def test_success_without_readiness_liveness_identity_or_cleanup_is_refused(self):
        for key, bad in (("ready", False), ("cleanup_complete", False), ("runtime", "/wrong"),
                         ("mode", "gdm"), ("stable_seconds", 0), ("ready_pid", True),
                          ("starttime", 0), ("mutter_mappings", []), ("shutdown_returncode", None),
                          ("shutdown_returncode", 137), ("shutdown_returncode", False)):
            document = self.valid()
            document[key] = bad
            with self.subTest(key=key), self.assertRaisesRegex(probe.ProbeError, "complete matching evidence"):
                probe.run_isolated(self.root, "ubuntu", run=lambda *a, **k:
                                   subprocess.CompletedProcess(a, 0, json.dumps(document), ""))

    def test_systemd_failure_overrides_worker_success(self):
        result = probe.run_isolated(self.root, "ubuntu", run=lambda *a, **k:
                                    subprocess.CompletedProcess(a, 1, json.dumps(self.valid()), ""))
        self.assertFalse(result["ok"])

    def test_timeout_stops_only_the_unique_test_service(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(argv, 60)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with self.assertRaisesRegex(probe.ProbeError, "timed out"):
            probe.run_isolated(self.root, "ubuntu", run=run)
        unit = next(arg.split("=", 1)[1] for arg in calls[0] if arg.startswith("--unit="))
        self.assertTrue(unit.startswith(probe.UNIT_PREFIX))
        self.assertEqual(calls[1], ["/usr/bin/systemctl", "--user", "stop", unit])


if __name__ == "__main__":
    unittest.main()
