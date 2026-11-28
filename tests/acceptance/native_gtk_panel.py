#!/usr/bin/env python3
"""Opt-in, generated-only native GTK panel/responsiveness check.

Uses the selected real display and read-only compositor status probes. It never
starts screen/audio capture, a CLI session, provider inference or screenshots.
Preferences/cache/data are temporary and no provider credentials are inherited.
Run --self-test for local guards without importing GTK or accessing a display.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "apps/socket_stream_desktop/ui"
ANSWER = "25; GENERATED-GTK-PANEL-7c93"
QUESTION = "Generated GTK preview: what is 17 + 8?"
SCHEMA = "floaty-native-gtk-panel-v1"
MAX_OUTPUT = 128 * 1024
PHASES = {"worker_start", "gtk_import", "gtk_init", "create_window", "window_created",
          "show_all", "window_shown", "generated_events", "main_loop", "cleanup", "complete"}


class CheckFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailure(code)


def emit(value):
    print(json.dumps(value, separators=(",", ":"), sort_keys=True), flush=True)


def phase(name):
    emit({"kind": "phase", "phase": name})


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_environment(args, root):
    """Allowlist display metadata; never copy the parent environment."""
    account = pwd.getpwuid(os.getuid())
    result = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": account.pw_dir, "USER": account.pw_name, "LOGNAME": account.pw_name,
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "GDK_BACKEND": args.backend, "XDG_RUNTIME_DIR": str(args.runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": args.session_bus_address,
        "XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_DESKTOP": "gnome",
        "XDG_SESSION_TYPE": "wayland" if args.wayland_socket else "x11",
        "SOCKET_STREAM_HOME": str(ROOT),
        "SOCKET_STREAM_CONFIG": str(root / "config" / "generated-only.json"),
        "SOCKET_STREAM_CLI": str(root / "disabled-cli-does-not-exist"),
        "SOCKET_STREAM_PROVIDER": "mock",
    }
    for variable, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                ("XDG_DATA_HOME", "data"), ("TMPDIR", "tmp")):
        target = root / directory
        target.mkdir(mode=0o700)
        result[variable] = str(target)
    if args.display:
        result["DISPLAY"] = args.display
    if args.wayland_socket:
        result["WAYLAND_DISPLAY"] = str(args.wayland_socket)
    if args.xauthority:
        result["XAUTHORITY"] = str(args.xauthority)
    return result


def validate_args(args):
    require(sys.platform == "linux" and os.getuid() != 0, "linux_nonroot_required")
    require(args.backend in ("x11", "wayland"), "invalid_backend")
    require(0 <= args.slow_status_ms <= 1000 and
            (args.slow_status_ms == 0 or args.slow_status_ms >= 250), "invalid_slow_status_delay")
    require(4 <= args.duration <= 15 and 15 <= args.timeout <= 90, "invalid_time_budget")
    require(args.timeout >= args.duration + 10, "insufficient_outer_timeout")
    runtime = args.runtime_dir
    require(runtime is not None and runtime.is_absolute(), "runtime_directory_required")
    info = runtime.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), "runtime_directory_owner")
    require(not info.st_mode & 0o022, "runtime_directory_permissions")
    expected_bus = "unix:path=" + str(runtime / "bus")
    require(args.session_bus_address == expected_bus, "explicit_runtime_session_bus_required")
    info = (runtime / "bus").stat()
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), "session_bus_socket_owner")
    if args.display:
        require(re.fullmatch(r":[0-9]+(?:\.[0-9]+)?", args.display) is not None, "local_x11_display_required")
    if args.backend == "x11":
        require(bool(args.display), "explicit_x11_display_required")
    if args.wayland_socket:
        require(args.wayland_socket.is_absolute() and args.wayland_socket.parent == runtime,
                "wayland_socket_scope")
        info = args.wayland_socket.stat()
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), "wayland_socket_owner")
    if args.backend == "wayland":
        require(args.wayland_socket is not None, "explicit_wayland_socket_required")
    if args.xauthority:
        require(args.xauthority.is_absolute(), "xauthority_path_required")
        info = args.xauthority.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "xauthority_owner")


def source_hashes():
    return {name: digest(UI / name) for name in
            ("linux-gtk.py", "background_refresh.py", "floating_overlay.py")}


def base_report():
    return {"schema": SCHEMA, "passed": False,
            "scope": "generated_panel_on_explicit_display_with_read_only_status_probes",
            "preferences_isolated": True, "provider_credentials_inherited": False,
            "screen_capture_started": False, "audio_capture_started": False,
            "cli_started": False, "provider_requests": 0, "screenshots_taken": False,
            "capture_exclusion_pixels_verified": False, "user_settings_modified": False}


def heartbeat_summary(ticks):
    gaps = [right - left for left, right in zip(ticks, ticks[1:])]
    maximum = max(gaps, default=0.0)
    return {"ticks": len(ticks), "maximum_gap_ms": round(maximum * 1000, 1),
            "interval_ms": 50, "passed": len(ticks) >= 20 and maximum <= 1.0}


class CallbackDiagnostics:
    """Keep bounded exception locations, never messages, source or locals."""
    def __init__(self):
        self.count = 0
        self.count_capped = False
        self.examples = []
        self.examples_truncated = False
        self._seen = set()

    def record(self, kind, _error, traceback):
        if self.count < 64:
            self.count += 1
        else:
            self.count_capped = True
        name = getattr(kind, "__name__", "Exception")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) is None:
            name = "Exception"
        frames = []
        while traceback is not None and len(frames) < 16:
            code = traceback.tb_frame.f_code
            filename = Path(code.co_filename).name
            function = code.co_name
            frames.append({
                "file": filename if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename) else "<runtime>",
                "line": int(traceback.tb_lineno),
                "function": function if re.fullmatch(r"[A-Za-z0-9_<>.]{1,80}", function) else "<function>",
            })
            traceback = traceback.tb_next
        item = {"type": name, "frames": frames, "frames_truncated": traceback is not None}
        key = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if key not in self._seen:
            if len(self.examples) < 32:
                self.examples.append(item)
                self._seen.add(key)
            else:
                self.examples_truncated = True

    def report(self):
        return {"count": self.count, "count_capped": self.count_capped,
                "examples": list(self.examples), "examples_truncated": self.examples_truncated}


def worker(args):
    """All GTK operations stay on this thread; parent owns the hard deadline."""
    import faulthandler
    phase("worker_start")
    faulthandler.enable()
    faulthandler.dump_traceback_later(12, repeat=True)
    report = base_report()
    report["source_sha256"] = source_hashes()
    app = None
    timers = []
    callbacks_failed = CallbackDiagnostics()
    gtk_counts = {"warning": 0, "critical": 0}
    original_hook = sys.excepthook
    gtk_handler = None
    main_thread = threading.get_ident()
    status_lock = threading.Lock()
    loop_started = threading.Event()
    status_facts = {"construction_started": False, "construction_on_main_thread": False,
                    "read_started": False, "read_on_main_thread": False, "read_completed": False,
                    "controlled_delay_ms": args.slow_status_ms, "delay_started": None,
                    "delay_finished": None, "controlled_delay_completed": False}
    try:
        phase("gtk_import")
        sys.path.insert(0, str(UI))
        import gi
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
        phase("gtk_init")
        require(Gtk.init_check()[0], "gtk_display_unavailable")
        sys.excepthook = callbacks_failed.record

        def gtk_log(_domain, level, _message, _data):
            key = "critical" if level & GLib.LogLevelFlags.LEVEL_CRITICAL else "warning"
            gtk_counts[key] += 1

        gtk_handler = GLib.log_set_handler("Gtk", GLib.LogLevelFlags.LEVEL_WARNING |
                                         GLib.LogLevelFlags.LEVEL_CRITICAL, gtk_log, None)
        import capture_rules
        original_init = capture_rules.CaptureExclusion.__init__
        original_status = capture_rules.CaptureExclusion.status

        def observed_init(instance, *values, **options):
            with status_lock:
                status_facts["construction_started"] = True
                status_facts["construction_on_main_thread"] |= threading.get_ident() == main_thread
            original_init(instance, *values, **options)

        def observed_status(instance):
            with status_lock:
                status_facts["read_started"] = True
                status_facts["read_on_main_thread"] |= threading.get_ident() == main_thread
                delay_once = args.slow_status_ms and status_facts["delay_started"] is None
            if delay_once:
                require(loop_started.wait(5), "controlled_probe_main_loop_not_started")
                with status_lock:
                    status_facts["delay_started"] = time.monotonic()
                time.sleep(args.slow_status_ms / 1000)
                with status_lock:
                    status_facts["delay_finished"] = time.monotonic()
                    status_facts["controlled_delay_completed"] = True
            value = original_status(instance)
            with status_lock:
                status_facts["read_completed"] = True
            return value

        capture_rules.CaptureExclusion.__init__ = observed_init
        capture_rules.CaptureExclusion.status = observed_status
        spec = importlib.util.spec_from_file_location("generated_native_gtk_controller", UI / "linux-gtk.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        phase("create_window")
        before = time.monotonic()
        app = controller.create_window()
        report["window_creation_ms"] = round((time.monotonic() - before) * 1000, 1)
        phase("window_created")
        forbidden = []

        def forbid_cli(*_values, **_options):
            forbidden.append(True)
            raise CheckFailure("cli_launch_forbidden_in_native_panel_check")

        app.run_cli = forbid_cli
        panel = app.floating
        maps = {"main": 0, "panel": 0}
        app.connect("map-event", lambda *_: maps.__setitem__("main", maps["main"] + 1))
        panel.window.connect("map-event", lambda *_: maps.__setitem__("panel", maps["panel"] + 1))
        phase("show_all")
        app.show_all()
        panel.show()
        phase("window_shown")
        app.active_provider = "Generated preview; no provider"
        phase("generated_events")
        events = (("session_started", {}),
                  ("question_detected", {"request_id": "generated", "text": QUESTION}),
                  ("answer_started", {"request_id": "generated"}),
                  ("answer_delta", {"request_id": "generated", "text": "Question: " + QUESTION + "\nAnswer: " + ANSWER}),
                  ("answer_completed", {"request_id": "generated"}))
        for sequence, (kind, fields) in enumerate(events, 1):
            app.apply_live_event(dict(schema=1, type=kind, session_id="generated-native-panel",
                                      source="file", sequence=sequence, **fields))
        loop = GLib.MainLoop()
        ticks = []
        panel_observed = {"mapped": False, "answer_buffer_matched": False}
        restore = {"iconify_requested": False, "iconification_observed": False,
                   "explicit_show_requested": False, "restored": False, "status": "pending"}
        start = time.monotonic()
        iconify_at = None
        show_at = None

        def check():
            nonlocal iconify_at, show_at
            now = time.monotonic()
            ticks.append(now)
            loop_started.set()
            buffer = panel.answer.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
            panel_observed["answer_buffer_matched"] |= text == ANSWER
            panel_observed["mapped"] |= bool(panel.window.get_mapped())
            gdk_window = panel.window.get_window()
            iconified = bool(gdk_window and gdk_window.get_state() & Gdk.WindowState.ICONIFIED)
            if iconify_at is None and now - start >= 1 and panel_observed["answer_buffer_matched"]:
                restore["iconify_requested"] = True
                panel.window.iconify()
                iconify_at = now
            elif iconify_at is not None and show_at is None:
                restore["iconification_observed"] |= iconified
                if iconified or now - iconify_at >= 1:
                    panel.show()
                    restore["explicit_show_requested"] = True
                    show_at = now
            elif show_at is not None:
                if restore["iconification_observed"]:
                    restore["restored"] |= not iconified and bool(panel.window.get_mapped())
                    restore["status"] = "passed" if restore["restored"] else "failed"
                else:
                    restore["status"] = "unsupported_iconification_not_observed"
            return True

        timers.append(GLib.timeout_add(50, check))
        timers.append(GLib.timeout_add(int(args.duration * 1000), lambda: (loop.quit(), False)[1]))
        phase("main_loop")
        loop.run()
        heartbeat = heartbeat_summary(ticks)
        with status_lock:
            facts = dict(status_facts)
        delay_start, delay_end = facts.pop("delay_started"), facts.pop("delay_finished")
        facts["heartbeats_during_controlled_delay"] = sum(
            delay_start <= tick <= delay_end for tick in ticks) if delay_start is not None and delay_end is not None else 0
        facts["real_status_probe_attempted"] = facts["read_started"] and (not args.slow_status_ms or facts["controlled_delay_completed"])
        facts["status_result_pending_at_cleanup"] = bool(app._capture_status_pending)
        policy_ok = not panel.window.get_accept_focus() and not panel.window.get_focus_on_map()
        report.update(heartbeat=heartbeat, status_probe=facts, panel={**panel_observed,
                      "visible": bool(panel.window.get_visible()), "mapped_at_end": bool(panel.window.get_mapped()),
                      "nonactivating_policy_preserved": policy_ok, "map_events": maps["panel"],
                      "restore": restore}, main_window_map_events=maps["main"],
                      gtk_messages=gtk_counts, callback_errors=callbacks_failed.count,
                      callback_error_details=callbacks_failed.report(),
                      readonly_share_probe_completed=bool(app.share_watcher.status().get("checked")))
        report["passed"] = bool(
            report["window_creation_ms"] <= 5000 and heartbeat["passed"] and all(panel_observed.values())
            and report["panel"]["visible"] and report["panel"]["mapped_at_end"] and policy_ok
            and facts["construction_started"] and not facts["construction_on_main_thread"]
            and facts["real_status_probe_attempted"] and not facts["read_on_main_thread"]
            and (not args.slow_status_ms or facts["heartbeats_during_controlled_delay"] >= 3)
            and restore["status"] in ("passed", "unsupported_iconification_not_observed")
            and not callbacks_failed.count and not gtk_counts["critical"] and not forbidden and app.proc is None)
        if not report["passed"]:
            report["failure_code"] = "native_panel_assertion_failed"
        phase("cleanup")
        for timer in timers[:1]:
            GLib.source_remove(timer)
        app.on_exit()
        report["gtk_cleanup_completed"] = bool(getattr(panel, "_destroyed", False))
        report["passed"] &= report["gtk_cleanup_completed"]
        capture_rules.CaptureExclusion.__init__ = original_init
        capture_rules.CaptureExclusion.status = original_status
        phase("complete")
    except CheckFailure as error:
        report["failure_code"] = str(error)
    except Exception as error:
        report["failure_code"] = "worker_" + type(error).__name__
    finally:
        if app is not None and not app._exiting:
            try:
                app.on_exit()
            except Exception:
                report["passed"] = False
                report["failure_code"] = "gtk_cleanup_failed"
        if gtk_handler is not None:
            GLib.log_remove_handler("Gtk", gtk_handler)
        report["callback_errors"] = callbacks_failed.count
        report["callback_error_details"] = callbacks_failed.report()
        if callbacks_failed.count:
            report["passed"] = False
            report.setdefault("failure_code", "native_callback_errors")
        sys.excepthook = original_hook
        faulthandler.cancel_dump_traceback_later()
    emit({"kind": "result", "report": report})
    return 0 if report["passed"] else 1


def stop_group(proc):
    """Reap the owned worker and also stop its remaining probe descendants."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            pass


def collect_worker(command, environment, timeout):
    proc = subprocess.Popen(command, env=environment, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    error = None
    selector = selectors.DefaultSelector()
    try:
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            if time.monotonic() >= deadline:
                error = "worker_deadline_exceeded"
                break
            ready = selector.select(min(0.1, max(0.0, deadline - time.monotonic())))
            for key, _mask in ready:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination = streams[key.data]
                if len(destination) + len(chunk) > MAX_OUTPUT:
                    error = "worker_output_limit"
                    break
                destination.extend(chunk)
            if error:
                break
            if proc.poll() is not None and not ready:
                break
        if error is None:
            try:
                proc.wait(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                error = "worker_exit_deadline_exceeded"
        return streams, proc.returncode, error
    finally:
        stop_group(proc)
        selector.close()
        proc.stdout.close()
        proc.stderr.close()


def parse_worker(streams, returncode, error):
    report = None
    phases = []
    for line in streams["stdout"].decode("utf-8", "replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("kind") == "phase" and value.get("phase") in PHASES:
            phases.append(value["phase"])
        elif value.get("kind") == "result" and isinstance(value.get("report"), dict):
            candidate = value["report"]
            if candidate.get("schema") == SCHEMA:
                report = candidate
    if report is None:
        report = base_report()
        report["failure_code"] = error or "worker_exited_without_report"
    if error or returncode != 0:
        report["passed"] = False
        report["failure_code"] = error or report.get("failure_code", "worker_failed")
    report["last_phase"] = phases[-1] if phases else "before_worker_start"
    report["completed_phases"] = list(dict.fromkeys(phases))[:len(PHASES)]
    if not report["passed"]:
        frames = []
        for match in re.finditer(r'File "([^"\n]+)", line ([0-9]+) in ([^\n]+)',
                                 streams["stderr"].decode("utf-8", "replace")):
            filename = Path(match.group(1)).name
            function = match.group(3).strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename) and re.fullmatch(r"[A-Za-z0-9_<>.]{1,80}", function):
                item = {"file": filename, "line": int(match.group(2)), "function": function}
                if item not in frames:
                    frames.append(item)
        report["owned_stack_locations"] = frames[:32]
        report["stderr_bytes_suppressed"] = len(streams["stderr"])
    return report


def self_test():
    args = argparse.Namespace(backend="x11", runtime_dir=Path("/synthetic/runtime"),
                              session_bus_address="unix:path=/synthetic/runtime/bus", display=":99",
                              wayland_socket=Path("/synthetic/runtime/wayland-0"), xauthority=None)
    with tempfile.TemporaryDirectory(prefix="floaty-gtk-selftest-") as temporary:
        env = private_environment(args, Path(temporary))
        assert "SOCKET_STREAM_API_KEY" not in env and "OPENAI_API_KEY" not in env
        assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
        assert env["SOCKET_STREAM_CONFIG"].startswith(temporary)
        assert env["GDK_BACKEND"] == "x11" and env["DISPLAY"] == ":99"
    assert heartbeat_summary([index / 20 for index in range(30)])["passed"]
    assert not heartbeat_summary([0, 5])["passed"]
    fake = {"stdout": bytearray(b'{"kind":"phase","phase":"create_window"}\n'),
            "stderr": bytearray(b'PRIVATE-ERROR-NOT-EXPORTED\n  File "/private/example.py", line 7 in create_window\n')}
    parsed = parse_worker(fake, None, "worker_deadline_exceeded")
    assert parsed["last_phase"] == "create_window" and not parsed["passed"]
    assert parsed["owned_stack_locations"] == [{"file": "example.py", "line": 7, "function": "create_window"}]
    assert "PRIVATE-ERROR" not in json.dumps(parsed) and "/private" not in json.dumps(parsed)
    command = [sys.executable, "-I", "-c", "import time; time.sleep(3)"]
    streams, code, error = collect_worker(command, {"PATH": "/usr/bin:/bin"}, 0.15)
    assert error == "worker_deadline_exceeded" and not streams["stdout"]
    diagnostic = CallbackDiagnostics()
    try:
        raise RuntimeError("SYNTHETIC-SECRET-MUST-NOT-APPEAR")
    except RuntimeError:
        kind, error, traceback = sys.exc_info()
        for _ in range(80):
            diagnostic.record(kind, error, traceback)
    rendered = json.dumps(diagnostic.report())
    assert diagnostic.count == 64 and diagnostic.count_capped and len(diagnostic.examples) == 1
    assert "SYNTHETIC-SECRET" not in rendered and str(ROOT) not in rendered
    for index in range(40):
        diagnostic.record(type("SyntheticError" + str(index), (Exception,), {}), None, None)
    assert len(diagnostic.examples) == 32 and diagnostic.examples_truncated
    emit({"schema": SCHEMA, "passed": True, "self_test": True, "guard_groups": 5,
          "gtk_or_display_used": False, "external_network_used": False})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", help="Explicit local X11 display, e.g. :0")
    parser.add_argument("--xauthority", type=Path, help="Explicit existing owned X11 authority file; never logged")
    parser.add_argument("--wayland-socket", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--session-bus-address")
    parser.add_argument("--backend", choices=("x11", "wayland"), default="x11")
    parser.add_argument("--slow-status-ms", type=int, default=0)
    parser.add_argument("--duration", type=float, default=6)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.worker:
        return worker(args)
    try:
        validate_args(args)
        with tempfile.TemporaryDirectory(prefix="floaty-native-gtk-", dir="/tmp") as directory:
            root = Path(directory)
            root.chmod(0o700)
            environment = private_environment(args, root)
            command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker",
                       "--backend", args.backend, "--duration", str(args.duration),
                       "--slow-status-ms", str(args.slow_status_ms)]
            report = parse_worker(*collect_worker(command, environment, args.timeout))
    except CheckFailure as error:
        report = {**base_report(), "failure_code": str(error)}
    except Exception as error:
        report = {**base_report(), "failure_code": "parent_" + type(error).__name__}
    emit(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
