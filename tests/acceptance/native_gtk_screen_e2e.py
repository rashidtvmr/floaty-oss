#!/usr/bin/env python3
"""Opt-in public practice page -> actual GTK Start -> provider -> answer panel.

Uses the explicitly selected real desktop session bus and display. Chrome has
an empty, owned profile and its sandbox remains enabled. Only that browser's
verified AT-SPI window is selected in the production GTK window chooser.
The real consent dialog, source preflight, CLI launch and JSON-event reader run.
No answer events or answers are injected. The host receives only a random
loopback credential; real inference needs the separate audited root relay.

Only bounded metadata and hashes are printed. --self-test uses no desktop,
Chrome, user settings, credentials or external network.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
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

import native_browser_screen_lab as browser
import native_gtk_panel as panel_check

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "apps/socket_stream_desktop/ui"
PUBLIC_URL = "https://leetcode.com/problems/two-sum/description/"
TITLE = "Two Sum"
MARKERS = ("Two Sum", "nums = [2,7,11,15]", "target = 9", "indices",
           "You may not use the same element twice")
SCHEMA = "floaty-native-gtk-public-screen-v1"
PHASES = {"worker_start", "desktop_bus", "chrome_start", "page_navigation",
          "page_ready", "native_window", "native_settle", "native_text", "gtk_create", "gtk_shown",
          "window_chooser", "start", "preflight", "cli_started", "answer_received",
          "answer_visible", "cleanup", "complete"}
MAX_OUTPUT = 128 * 1024
NATIVE_SETTLE_SECONDS = 10
NATIVE_SETTLE_POLL_SECONDS = .25


class CheckFailure(Exception):
    pass


def require(value, code):
    if not value:
        raise CheckFailure(code)


def emit(value):
    print(json.dumps(value, separators=(",", ":"), sort_keys=True), flush=True)


def phase(value):
    require(value in PHASES, "invalid_phase")
    emit({"kind": "phase", "phase": value})


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def compact(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def has_markers(value):
    text = compact(value)
    return all(compact(marker) in text for marker in MARKERS)


def validate_args(args):
    require(sys.platform == "linux" and os.getuid() != 0, "linux_nonroot_required")
    require(args.backend in ("x11", "wayland"), "invalid_backend")
    require(100 <= args.timeout <= 240 and 0 <= args.hold_seconds <= 30, "invalid_time_limit")
    runtime = args.runtime_dir
    require(runtime is not None and runtime.is_absolute(), "runtime_directory_required")
    info = runtime.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o022, "runtime_directory_invalid")
    require(args.session_bus_address == "unix:path=" + str(runtime / "bus"),
            "explicit_real_session_bus_required")
    info = (runtime / "bus").stat()
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), "session_bus_socket_invalid")
    require(args.wayland_socket and args.wayland_socket.is_absolute()
            and args.wayland_socket.parent == runtime, "explicit_wayland_socket_required")
    info = args.wayland_socket.stat()
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), "wayland_socket_invalid")
    if args.backend == "x11":
        require(args.display and re.fullmatch(r":[0-9]+(?:\.[0-9]+)?", args.display),
                "explicit_local_x11_display_required")
    if args.xauthority:
        require(args.xauthority.is_absolute(), "xauthority_path_invalid")
        info = args.xauthority.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "xauthority_owner_invalid")
    for name in ("chrome", "cli"):
        path = getattr(args, name)
        require(path and path.is_absolute() and path.is_file() and os.access(path, os.X_OK),
                name + "_binary_invalid")
        with path.open("rb") as source:
            require(source.read(4) == b"\x7fELF", name + "_must_be_elf")
    if not args.inspect_only:
        require(args.relay_dir is not None, "explicit_relay_directory_required")
    if args.relay_dir is not None:
        from screen_model_relay import RelayDirectory
        directory = RelayDirectory(args.relay_dir)
        directory.close()


def environment(args, root):
    result = panel_check.private_environment(args, root)
    result.update(SOCKET_STREAM_CLI=str(args.cli))
    # These are all isolated defaults, never an existing user's saved profile.
    result.pop("SOCKET_STREAM_PROVIDER", None)
    return result


def process_identity(pid):
    try:
        directory = Path("/proc") / str(pid)
        info = directory.stat()
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "uid": info.st_uid, "ppid": int(fields[1]),
                "start": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def set_subreaper(value=None):
    """Adopt only our orphaned descendants; never attach to existing apps."""
    require(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "owned_pidfd_cleanup_unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    require(libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0, "owned_subreaper_query_failed")
    require(libc.prctl(36, 1 if value is None else value, 0, 0, 0) == 0, "owned_subreaper_setup_failed")
    return previous.value


def cleanup_children():
    """Reap direct/adopted children even after their original leader exits.

    The parent and worker each enable CHILD_SUBREAPER before spawning. A child
    whose PPID is this process is consequently ours; unrelated desktop apps
    are never eligible. PIDFD plus UID/start/PPID rechecks prevent PID reuse.
    Killing an owned parent adopts its surviving children for the next pass.
    """
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        children = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                item = process_identity(int(entry.name))
                if item and item["ppid"] == os.getpid():
                    children.append(item)
        if not children:
            return
        for expected in children:
            require(expected["uid"] == os.getuid(), "owned_child_changed_uid")
            descriptor = None
            try:
                descriptor = os.pidfd_open(expected["pid"], 0)
                actual = process_identity(expected["pid"])
                if actual == expected and actual["ppid"] == os.getpid():
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
            except ProcessLookupError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            try:
                os.waitpid(expected["pid"], os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(.02)
    raise CheckFailure("owned_descendant_cleanup_deadline")


def is_owned_descendant(pid, ancestor, *, read=process_identity):
    seen = set()
    for _ in range(24):
        item = read(pid)
        if not item or item["uid"] != os.getuid() or pid in seen:
            return False
        if pid == ancestor["pid"]:
            return item["start"] == ancestor["start"]
        seen.add(pid)
        pid = item["ppid"]
        if pid <= 1:
            return False
    return False


def register_owned(root, name, process):
    require(name in ("chrome", "cli"), "invalid_owned_process_name")
    identity = process_identity(process.pid)
    require(identity and identity["uid"] == os.getuid(), "owned_process_identity_missing")
    path = root / ("owned-" + name + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(identity, target)
    return identity


def stop_registered(root):
    """Parent watchdog kills only the two recorded, unchanged owned groups."""
    failures = []
    for name in ("cli", "chrome"):
        path = root / ("owned-" + name + ".json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and not info.st_mode & 0o077 and info.st_size <= 1024, "owned_registry_invalid")
                expected = json.loads(source.read(1025))
            actual = process_identity(expected["pid"])
            if actual and actual["uid"] == expected["uid"] == os.getuid() and actual["start"] == expected["start"]:
                try:
                    os.killpg(expected["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except FileNotFoundError:
            pass
        except Exception:
            failures.append(name)
    require(not failures, "owned_watchdog_cleanup_failed")


def spawn_chrome(args, root, child_env):
    read_child, write_parent = os.pipe()
    read_parent, write_child = os.pipe()
    wrapper = root / "owned-chrome-pipe.py"
    wrapper.write_text(
        "import fcntl,os,sys\n"
        "a,b=map(int,sys.argv[1:3])\n"
        "r=fcntl.fcntl(a,fcntl.F_DUPFD_CLOEXEC,10)\n"
        "w=fcntl.fcntl(b,fcntl.F_DUPFD_CLOEXEC,10)\n"
        "os.dup2(r,3,inheritable=True)\nos.dup2(w,4,inheritable=True)\n"
        "for fd in {a,b,r,w}:\n"
        " if fd not in (3,4): os.close(fd)\n"
        "os.execv(sys.argv[3],sys.argv[3:])\n", encoding="utf-8")
    argv = ["--ozone-platform=wayland", "--remote-debugging-pipe",
            "--user-data-dir=" + str(root / "chrome-profile"), "--profile-directory=Default",
            "--password-store=basic", "--no-startup-window", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--disable-component-update", "--disable-sync", "--disable-extensions",
            "--disable-breakpad", "--disable-crash-reporter", "--window-size=1200,1000"]
    if args.force_renderer_accessibility:
        argv.append("--force-renderer-accessibility")
    chrome_env = dict(child_env)
    chrome_env.pop("DISPLAY", None)
    chrome_env.pop("GDK_BACKEND", None)
    try:
        process = subprocess.Popen(["/usr/bin/python3", str(wrapper), str(read_child), str(write_child),
                                    str(args.chrome), *argv], env=chrome_env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   pass_fds=(read_child, write_child), start_new_session=True)
    except BaseException:
        os.close(read_parent)
        os.close(write_parent)
        raise
    finally:
        os.close(read_child)
        os.close(write_child)
    try:
        identity = register_owned(root, "chrome", process)
    except BaseException:
        browser.stop_owned(process)
        os.close(read_parent)
        os.close(write_parent)
        raise
    return process, browser.PipeCDP(read_parent, write_parent), identity


def desktop_accessibility_bus():
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    address = session.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
                                "GetAddress", None, GLib.VariantType.new("(s)"),
                                Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
    require(isinstance(address, str) and address.startswith("unix:"), "desktop_a11y_address_invalid")
    connection = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)

    def owner(window_id, *, timeout_ms=1500):
        identity = json.loads(window_id)
        require(isinstance(identity, list) and len(identity) == 2
                and isinstance(identity[0], str) and re.fullmatch(r":[0-9]+\.[0-9]+", identity[0])
                and isinstance(identity[1], str) and identity[1].startswith("/"), "native_window_identity_invalid")
        return connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                    "GetConnectionUnixProcessID", GLib.Variant("(s)", (identity[0],)),
                                    GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, timeout_ms, None).unpack()[0]

    return address, owner


def page_probe(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    body = compact(metadata.get("text", ""))
    return {"expected_url": metadata.get("url") == PUBLIC_URL,
            "title_has_question": TITLE.casefold() in str(metadata.get("title", "")).casefold(),
            "document_ready": metadata.get("ready") == "complete",
            "required_markers_present": sum(compact(marker) in body for marker in MARKERS),
            "required_marker_count": len(MARKERS)}


def open_page(cdp, report):
    cdp.expected_url = PUBLIC_URL
    startup = [value for value in cdp.call("Target.getTargets").get("targetInfos", [])
               if value.get("type") == "page"]
    require(len(startup) <= 4 and all(browser.startup_url_summary(value.get("url"), PUBLIC_URL)["url_category"]
            in ("empty", "about_blank", "browser_internal", "data_startup") for value in startup),
            "unexpected_owned_startup_page")
    target = cdp.call("Target.createTarget", {"url": "about:blank", "newWindow": True}, timeout=20)["targetId"]
    require(all(value.get("targetId") != target for value in startup), "owned_target_not_new")
    for value in startup:
        require(cdp.call("Target.closeTarget", {"targetId": value["targetId"]}).get("success"),
                "owned_startup_close_failed")
    cdp.target_id = target
    session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    for method in ("Runtime.enable", "Page.enable", "Runtime.runIfWaitingForDebugger"):
        cdp.call(method, session=session)
    result = cdp.call("Page.navigate", {"url": PUBLIC_URL}, session=session, timeout=25)
    require(not result.get("errorText") and not result.get("isDownload"), "public_page_navigation_failed")
    deadline = time.monotonic() + 30
    metadata = {}
    while time.monotonic() < deadline:
        try:
            metadata = cdp.evaluate(session, "(() => ({url:location.href,title:document.title,"
                "ready:document.readyState,text:(document.body?.innerText||'').slice(0,200000)}))()")
        except browser.LabFailure:
            time.sleep(.15)
            continue
        report["page_probe"] = page_probe(metadata)
        if (isinstance(metadata, dict) and metadata.get("url") == PUBLIC_URL
                and TITLE.casefold() in metadata.get("title", "").casefold()
                and metadata.get("ready") == "complete" and has_markers(metadata.get("text", ""))):
            return session, {"url_verified": True, "title_verified": True, "public_question_markers": True,
                             "document_text_sha256": sha(metadata["text"])}
        time.sleep(.15)
    raise CheckFailure("public_practice_question_not_ready")


def select_owned_window(child_env, owner, chrome_identity):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        windows = browser.read_helper(child_env, "windows")
        candidates = []
        for window in windows:
            if TITLE.casefold() not in str(window.get("title", "")).casefold():
                continue
            try:
                pid = owner(window["id"])
            except Exception:
                continue
            if is_owned_descendant(pid, chrome_identity):
                candidates.append(window)
        require(len(candidates) <= 1, "owned_browser_window_ambiguous")
        if candidates:
            window = candidates[0]
            label = (window.get("application", "") + " — " + window["title"]).lstrip(" —")
            require(sum((value.get("application", "") + " — " + value.get("title", "")).lstrip(" —") == label
                        for value in windows) == 1, "window_chooser_label_ambiguous")
            return window, label
        time.sleep(.2)
    raise CheckFailure("owned_browser_accessibility_window_unavailable")


def read_native_tree(environment, *, timeout):
    """Read the production native helper with the remaining settle budget."""
    result = subprocess.run([browser.SYSTEM_PYTHON, str(browser.HELPER), "--mode", "tree"],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout)
    require(result.returncode == 0, "native_helper_exit_" + str(result.returncode))
    require(len(result.stdout) <= 1_048_576, "native_helper_output_too_large")
    return json.loads(result.stdout)


def settle_native_tree(environment, window_id, chrome_identity, owner, report, *,
                       read=read_native_tree, clock=time.monotonic, sleep=time.sleep,
                       timeout=NATIVE_SETTLE_SECONDS):
    """Wait only for native content in the already selected owned window.

    Chrome may expose its frame before its renderer tree is published. Reads
    retain the same native target and owner; missing text alone permits another
    read. No DOM content, new target, accessibility override or provider is used.
    """
    require(environment.get("SOCKET_STREAM_ATSPI_TARGET") == window_id,
            "native_settle_target_mismatch")
    require(0 < timeout <= NATIVE_SETTLE_SECONDS, "native_settle_budget_invalid")
    started = clock()
    deadline = started + timeout
    stats = {"timeout_seconds": timeout, "poll_interval_ms": NATIVE_SETTLE_POLL_SECONDS * 1000,
             "attempts": 0, "completed_reads": 0, "owner_checks": 0, "elapsed_ms": 0,
             "max_read_ms": 0, "first_nodes": None, "first_marker_count": None,
             "ready": False, "deadline_reached": False}
    report["native_settle"] = stats
    expected_pid = None

    def remaining():
        value = deadline - clock()
        if value <= 0:
            stats["deadline_reached"] = True
            raise CheckFailure("native_practice_question_unreadable")
        return value

    try:
        while True:
            owner_timeout = max(1, min(1500, int(remaining() * 1000)))
            stats["owner_checks"] += 1
            pid = owner(window_id, timeout_ms=owner_timeout)
            require(type(pid) is int and is_owned_descendant(pid, chrome_identity)
                    and (expected_pid is None or pid == expected_pid), "native_capture_scope_mismatch")
            expected_pid = pid
            read_timeout = min(7, remaining())
            stats["attempts"] += 1
            read_started = clock()
            try:
                tree = read(environment, timeout=read_timeout)
            except subprocess.TimeoutExpired:
                stats["deadline_reached"] = clock() >= deadline
                raise CheckFailure("native_practice_question_read_timeout") from None
            finally:
                stats["max_read_ms"] = max(stats["max_read_ms"], round((clock() - read_started) * 1000, 1))
            stats["completed_reads"] += 1
            require(isinstance(tree, dict) and tree.get("schema") == 1 and tree.get("source") == "atspi",
                    "native_capture_invalid")
            require(tree.get("window_id") == window_id and type(tree.get("pid")) is int
                    and tree["pid"] == expected_pid and is_owned_descendant(tree["pid"], chrome_identity),
                    "native_capture_scope_mismatch")
            nodes = tree.get("nodes")
            require(isinstance(nodes, list) and all(isinstance(node, dict) for node in nodes),
                    "native_capture_invalid")
            text = "\n".join(str(node.get(key, "")) for node in nodes for key in ("name", "text"))
            normalized = compact(text)
            marker_count = sum(compact(marker) in normalized for marker in MARKERS)
            report["native_text"] = {"question_markers_present": has_markers(text), "nodes": len(nodes),
                                     "truncated": bool(tree.get("truncated")), "text_sha256": sha(text),
                                     "required_markers_present": marker_count,
                                     "required_marker_count": len(MARKERS)}
            if stats["first_nodes"] is None:
                stats.update(first_nodes=len(nodes), first_marker_count=marker_count)
            wait_budget = remaining()
            if has_markers(text):
                stats["ready"] = True
                return tree
            sleep(min(NATIVE_SETTLE_POLL_SECONDS, wait_budget))
    finally:
        stats["elapsed_ms"] = round((clock() - started) * 1000, 1)


class LiveAudit:
    """Passive observation of real reader callbacks, never event injection."""
    def __init__(self):
        self.events = Counter()
        self.session_started_at = None
        self.answer = ""
        self.completed = False
        self.request_id = None
        self.question_markers = False
        self.failure = None

    def observe(self, event):
        kind = event.get("type")
        if kind not in ("session_started", "session_stopped", "session_paused", "session_resumed",
                        "question_detected", "answer_started", "answer_delta", "answer_completed",
                        "error", "capture_started", "capture_completed", "diagnostic", "token_usage"):
            kind = "other"
        self.events[kind] += 1
        if kind == "session_started":
            self.session_started_at = time.monotonic()
        if kind == "question_detected":
            self.question_markers |= has_markers(event.get("text", ""))
        if kind == "answer_started":
            require(self.events[kind] == 1, "multiple_model_answers_started")
            self.request_id = event.get("request_id")
        if kind == "answer_delta":
            require(event.get("request_id") == self.request_id and isinstance(event.get("text"), str),
                    "answer_delta_identity_invalid")
            self.answer += event["text"]
            require(len(self.answer.encode()) <= 65536, "answer_text_limit")
        if kind == "answer_completed":
            require(event.get("request_id") == self.request_id and bool(self.answer.strip()),
                    "empty_or_unbound_completion")
            self.completed = True
        if kind == "error":
            self.failure = "production_cli_or_preflight_error"


def needs_manual_capture(audit, provider, already_clicked, now):
    return bool(audit.session_started_at is not None and not audit.events["answer_started"]
                and now - audit.session_started_at >= 7 and not already_clicked
                and not provider.requests and not provider.lock.locked() and provider.failure is None)


def gtk_run(args, root, child_env, window, chooser_label, provider, report):
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    require(Gtk.init_check()[0], "gtk_display_unavailable")
    display = Gdk.Display.get_default()
    display_type = display.__gtype__.name if display is not None else ""
    report["gtk_backend"] = {"GdkX11Display": "x11", "GdkWaylandDisplay": "wayland"}.get(display_type, "unknown")
    require(report["gtk_backend"] == args.backend, "gtk_backend_mismatch")
    sys.path.insert(0, str(UI))
    import source_preflight
    from answer_format import answer_parts, markdown_runs

    audit = LiveAudit()
    callbacks = panel_check.CallbackDiagnostics()
    original_hook = sys.excepthook
    sys.excepthook = callbacks.record
    preflight = {"calls": 0, "screen_only": False, "on_worker": False, "errors": None}
    original_check = source_preflight.check_sources
    main_thread = threading.get_ident()

    def checked(audio, screen, source, device, repository, **kwargs):
        preflight.update(calls=preflight["calls"] + 1, screen_only=audio is False and screen is True,
                         on_worker=threading.get_ident() != main_thread)
        require(preflight["screen_only"] and preflight["calls"] == 1, "unexpected_preflight_sources")
        result = original_check(audio, screen, source, device, repository, **kwargs)
        preflight["errors"] = len(result[0])
        return result

    source_preflight.check_sources = checked
    app = None
    process = None
    timer = None
    loop = GLib.MainLoop()
    state = {"chooser_accepted": False, "start_clicked": False, "consent_accepted": False,
             "launches": 0, "manual_capture_clicked": False, "failure": None, "visible_at": None,
             "completed_at": None}
    ticks = []
    try:
        spec = importlib.util.spec_from_file_location("native_public_gtk_controller", UI / "linux-gtk.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        config = Path(os.environ["SOCKET_STREAM_CONFIG"])
        require(config.parent == root / "config" and not config.exists() and not config.is_symlink(),
                "isolated_default_config_required")
        report["capture_configuration"] = {"isolated_defaults": True, "a11y_mode": "tree",
                                           "sources": {"audio": False, "screen": True}}
        phase("gtk_create")
        app = controller.create_window()
        app.env.update(SOCKET_STREAM_PROVIDER="openai-compatible",
                       SOCKET_STREAM_BASE_URL=f"http://127.0.0.1:{provider.port}/v1",
                       SOCKET_STREAM_MODEL=provider.model, SOCKET_STREAM_API_KEY=provider.token)
        app.fallback_profile = ""
        app.provider.set_active(3)
        app.sync_provider_label()
        app.audio_check.set_active(False)
        app.screen_check.set_active(True)
        app.interval.set_value(3600)
        app.force_ocr.set_active(False)
        app.portal.set_active(False)
        original_event = app.apply_live_event
        original_launch = app.run_cli

        def event_received(event):
            try:
                audit.observe(event)
            except CheckFailure as error:
                state["failure"] = str(error)
            return original_event(event)

        def launch(arguments):
            nonlocal process
            state["launches"] += 1
            require(state["launches"] == 1 and arguments[0] == "observe-live"
                    and "--allow-ocr" not in arguments and "--portal" not in arguments,
                    "unexpected_production_launch")
            require(app.question_window_target == window["id"] and not app.audio_check.get_active()
                    and app.screen_check.get_active() and not app.fallback_profile,
                    "production_launch_scope_changed")
            require(not config.exists() and not config.is_symlink(), "isolated_capture_config_changed")
            result = original_launch(arguments)
            if app.proc is not None:
                process = app.proc
                register_owned(root, "cli", process)
                phase("cli_started")
            return result

        app.apply_live_event = event_received
        app.run_cli = launch
        app.show_all()
        phase("gtk_shown")
        start = time.monotonic()

        def children(widget):
            yield widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    yield from children(child)

        def fail(code):
            state["failure"] = code
            loop.quit()

        def tick():
            now = time.monotonic()
            ticks.append(now)
            try:
                require(not callbacks.count, "gtk_callback_error")
                require(not state["failure"], state["failure"] or "unexpected_failure")
                require(not audit.failure, audit.failure or "production_error")
                require(provider.failure is None, "public_provider_guard_failed")
                require(now - start < 125, "gtk_answer_deadline")
                dialogs = [item for item in Gtk.Window.list_toplevels()
                           if isinstance(item, Gtk.Dialog) and item.get_visible()]
                for dialog in dialogs:
                    if dialog.get_title() == "Choose question window" and not state["chooser_accepted"]:
                        choices = [item for item in children(dialog) if isinstance(item, Gtk.ComboBoxText)]
                        require(len(choices) == 1, "window_chooser_widget_mismatch")
                        rows = [(row[0], row[1]) for row in choices[0].get_model()]
                        matches = [identity for label, identity in rows if label == chooser_label]
                        require(len(matches) == 1, "window_chooser_target_mismatch")
                        choices[0].set_active_id(matches[0])
                        state["chooser_accepted"] = True
                        dialog.response(Gtk.ResponseType.OK)
                        # Let the production dialog.run() return and apply the
                        # selected native ID before inspecting controller state.
                        return True
                    elif isinstance(dialog, Gtk.MessageDialog) and state["start_clicked"] and not state["consent_accepted"]:
                        require(dialog.get_property("text") == "Start selected sources?"
                                and dialog.get_transient_for() is app, "unexpected_consent_dialog")
                        explanation = dialog.get_property("secondary-text") or ""
                        require("selected window: " + window["title"] in explanation
                                and "Start system audio" not in explanation
                                and "Start microphone audio" not in explanation, "consent_scope_mismatch")
                        state["consent_accepted"] = True
                        dialog.response(Gtk.ResponseType.OK)
                        return True
                if state["chooser_accepted"] and not state["start_clicked"]:
                    require(app.question_window_target == window["id"], "selected_target_changed")
                    require(app.start_button.get_sensitive(), "start_button_disabled")
                    state["start_clicked"] = True
                    phase("start")
                    GLib.idle_add(lambda: (app.start_button.emit("clicked"), False)[1])
                if needs_manual_capture(audit, provider, state["manual_capture_clicked"], now):
                    state["manual_capture_clicked"] = True
                    app.floating.capture_button.emit("clicked")
                if audit.completed and state["visible_at"] is None:
                    if state["completed_at"] is None:
                        state["completed_at"] = now
                        phase("answer_received")
                    require(len(provider.answers) == 1 and audit.answer.strip() == provider.answers[0].strip(),
                            "cli_answer_does_not_match_relay")
                    live_view = app.live_state.display_snapshot()
                    raw = live_view["answer"]
                    require(raw == audit.answer, "live_state_does_not_match_cli")
                    require(live_view["source"] in ("accessibility", "screen"), "answer_source_is_not_screen")
                    expected = "".join(text for text, _tag in markdown_runs(answer_parts(raw)[1]))
                    buffer = app.floating.answer.get_buffer()
                    rendered = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
                    require(expected.strip(), "empty_rendered_answer")
                    if rendered != expected:
                        # Production rendering deliberately coalesces streams
                        # on an 80ms timer. Observe its real completion; never
                        # force _flush_answer or write the GTK buffer ourselves.
                        require(now - state["completed_at"] < 2, "gtk_answer_buffer_mismatch")
                        return True
                    require(app.floating.window.get_mapped() and app.floating.window.get_visible()
                            and app.floating.answer_stack.get_visible_child_name() == "answer", "answer_panel_not_visible")
                    report["answer_panel"] = {"visible": True, "mapped": True, "real_buffer_matches_completed_response": True,
                                             "completed_text_sha256": sha(raw.strip()), "rendered_buffer_sha256": sha(rendered),
                                             "rendered_characters": len(rendered), "direct_event_injection": False,
                                             "source": live_view["source"]}
                    state["visible_at"] = now
                    phase("answer_visible")
                    app.stop()
                if state["visible_at"] is not None and now - state["visible_at"] >= args.hold_seconds:
                    loop.quit()
                    return False
                if state["launches"] and app.proc is None and not audit.completed:
                    fail("cli_exited_without_completed_answer")
                    return False
            except CheckFailure as error:
                fail(str(error))
                return False
            except Exception:
                fail("gtk_orchestration_error")
                return False
            return True

        timer = GLib.timeout_add(50, tick)
        phase("window_chooser")
        GLib.idle_add(lambda: (app.floating.window_button.emit("clicked"), False)[1])
        loop.run()
        report.update(start_path={key: state[key] for key in ("chooser_accepted", "start_clicked", "consent_accepted",
                                                            "launches", "manual_capture_clicked")},
                      source_preflight=preflight, cli_event_counts=dict(audit.events),
                      heartbeat=panel_check.heartbeat_summary(ticks), callback_errors=callbacks.count,
                      callback_error_details=callbacks.report(),
                      actual_question_markers_in_cli_event=audit.question_markers,
                      provider=provider.report())
        require(not state["failure"], state["failure"] or "gtk_flow_failed")
        require(all((state["chooser_accepted"], state["start_clicked"], state["consent_accepted"],
                     audit.completed, report.get("answer_panel"), preflight["calls"] == 1,
                     preflight["screen_only"], preflight["on_worker"], preflight["errors"] == 0,
                     audit.question_markers,
                     audit.events["answer_started"] == 1, audit.events["answer_completed"] == 1,
                     len(provider.requests) == 1, len(provider.answers) == 1, not callbacks.count)),
                "gtk_start_to_answer_assertion_failed")
    finally:
        phase("cleanup")
        cleanup_failed = False
        if timer is not None:
            try:
                GLib.source_remove(timer)
            except Exception:
                pass
        if app is not None:
            try:
                app.on_exit()
            except Exception:
                cleanup_failed = True
        if process is not None:
            try:
                browser.stop_owned(process)
            except Exception:
                cleanup_failed = True
        if app is not None:
            try:
                deadline = time.monotonic() + 2
                context = GLib.MainContext.default()
                while not getattr(app.floating, "_destroyed", False) and time.monotonic() < deadline:
                    context.iteration(False)
                    time.sleep(.01)
                report["gtk_cleanup_completed"] = bool(getattr(app.floating, "_destroyed", False))
            except Exception:
                cleanup_failed = True
        source_preflight.check_sources = original_check
        report.update(callback_errors=callbacks.count, callback_error_details=callbacks.report())
        sys.excepthook = original_hook
        require(not cleanup_failed and not callbacks.count, "gtk_cleanup_callback_failed")


def worker(args):
    root = Path(args.private_root)
    report = {"schema": SCHEMA, "passed": False, "public_practice_url": PUBLIC_URL,
              "real_desktop_session_bus": True, "private_accessibility_bus": False,
              "owned_empty_chrome_profile": True, "chrome_sandbox_disabled": False,
              "forced_renderer_accessibility": args.force_renderer_accessibility,
              "gtk_backend_requested": args.backend, "browser_backend": "wayland",
              "automatic_foreground_selection_tested": False, "selected_window_only": True,
              "audio_capture_started": False, "screenshots_taken": False,
              "provider_credentials_inherited": False, "user_settings_modified": False,
              "source_sha256": panel_check.source_hashes(), "cli_sha256": browser.sha256(args.cli)}
    process = cdp = provider = None
    previous_subreaper = None
    try:
        phase("worker_start")
        previous_subreaper = set_subreaper()
        require(root.is_absolute() and root.stat().st_uid == os.getuid()
                and root.stat().st_mode & 0o077 == 0, "private_runtime_invalid")
        child_env = dict(os.environ)
        phase("desktop_bus")
        address, owner = desktop_accessibility_bus()
        child_env["AT_SPI_BUS_ADDRESS"] = address
        os.environ["AT_SPI_BUS_ADDRESS"] = address
        phase("chrome_start")
        process, cdp, identity = spawn_chrome(args, root, child_env)
        version = cdp.call("Browser.getVersion", timeout=20).get("product", "")
        report["browser_version"] = version if re.fullmatch(r"[A-Za-z]+/[0-9.]+", version) else "unreported"
        phase("page_navigation")
        _session, metadata = open_page(cdp, report)
        report["public_page"] = metadata
        phase("page_ready")
        window, label = select_owned_window(child_env, owner, identity)
        report["selected_window_owned_by_browser"] = True
        phase("native_window")
        capture_env = dict(child_env, SOCKET_STREAM_ATSPI_TARGET=window["id"])
        phase("native_settle")
        tree = settle_native_tree(capture_env, window["id"], identity, owner, report)
        phase("native_text")
        if not args.inspect_only:
            from public_practice_relay import PublicPracticeProvider

            def current_source_tree():
                # Only the owned pipe and native target are consulted here.
                # Recheck ownership/page immediately before the provider sees
                # the actual CLI request, without substituting DOM text.
                metadata = cdp.evaluate(_session, "(() => ({url:location.href,title:document.title,"
                    "text:(document.body?.innerText||'').slice(0,200000)}))()")
                require(isinstance(metadata, dict) and metadata.get("url") == PUBLIC_URL
                        and TITLE.casefold() in metadata.get("title", "").casefold()
                        and has_markers(metadata.get("text", "")), "public_page_changed_before_request")
                require(is_owned_descendant(owner(window["id"]), identity), "browser_owner_changed_before_request")
                actual = browser.read_helper(capture_env)
                require(actual.get("window_id") == window["id"] and actual.get("pid") == tree.get("pid")
                        and is_owned_descendant(actual.get("pid"), identity), "native_source_changed_before_request")
                return actual

            provider = PublicPracticeProvider(args.relay_dir, source_tree=current_source_tree,
                window_id=window["id"], window_title=window["title"], browser_pid=tree["pid"],
                page_url=PUBLIC_URL, user_bus_verified=True, owned_profile_verified=True)
            gtk_run(args, root, child_env, window, label, provider, report)
            require(report.get("gtk_cleanup_completed"), "gtk_cleanup_incomplete")
        else:
            report.update(inspection_only=True, provider_requests=0, gtk_start_tested=False)
        report["passed"] = True
        phase("complete")
    except CheckFailure as error:
        report["failure_code"] = str(error)
    except Exception as error:
        report["failure_code"] = "worker_" + type(error).__name__
    finally:
        for action in (lambda: provider.close() if provider else None,
                       lambda: cdp.close() if cdp else None,
                       lambda: browser.stop_owned(process)):
            try:
                action()
            except Exception:
                report["passed"] = False
                report.setdefault("failure_code", "owned_cleanup_failed")
        if previous_subreaper is not None:
            try:
                cleanup_children()
                report["owned_descendant_cleanup_completed"] = True
            except Exception:
                report["passed"] = False
                report.setdefault("failure_code", "owned_descendant_cleanup_failed")
            finally:
                set_subreaper(previous_subreaper)
    emit({"kind": "result", "report": report})
    return 0 if report["passed"] else 1


def self_test_native_settle():
    """Deterministic publication, deadline and ownership regressions; no bus."""
    from unittest import mock
    uid = os.getuid()
    records = {10: {"pid": 10, "uid": uid, "ppid": 1, "start": 20},
               11: {"pid": 11, "uid": uid, "ppid": 10, "start": 21},
               13: {"pid": 13, "uid": uid, "ppid": 10, "start": 23}}
    window_id = '[":1.77","/owned/frame"]'
    env = {"SOCKET_STREAM_ATSPI_TARGET": window_id, "PATH": "/usr/bin:/bin"}
    frame = {"schema": 1, "source": "atspi", "window_id": window_id, "pid": 11,
             "truncated": False, "nodes": [{"role": "frame", "name": TITLE}]}
    complete = dict(frame, nodes=[*frame["nodes"], {"role": "paragraph", "text": "\n".join(MARKERS)}])
    descendant = is_owned_descendant

    def scenario(values, *, expected=None, owners=(11,), read_delay=.03, source_env=None):
        now = [100.0]
        report, reads, ownership = {}, [], []
        actual = None

        def clock():
            return now[0]

        def sleep(delay):
            require(0 < delay <= NATIVE_SETTLE_POLL_SECONDS, "self_test_settle_sleep_budget")
            now[0] += delay

        def owner(selected, *, timeout_ms):
            require(selected == window_id and 0 < timeout_ms <= 1000,
                    "self_test_settle_owner_scope")
            index = len(ownership)
            ownership.append((selected, timeout_ms))
            now[0] += .01
            return owners[min(index, len(owners) - 1)]

        def read(selected_env, *, timeout):
            require(selected_env == env and 0 < timeout <= 101.0 - now[0],
                    "self_test_settle_read_scope_or_budget")
            index = len(reads)
            reads.append(timeout)
            now[0] += read_delay
            value = values[min(index, len(values) - 1)]
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch(__name__ + ".is_owned_descendant",
                        side_effect=lambda pid, ancestor: descendant(pid, ancestor, read=records.get)):
            try:
                actual = settle_native_tree(env if source_env is None else source_env,
                    window_id, records[10], owner, report, read=read, clock=clock, sleep=sleep, timeout=1)
            except CheckFailure as error:
                require(expected == str(error), "self_test_settle_unexpected_failure")
            else:
                require(expected is None, "self_test_settle_rejection_missing")
        require("/owned/frame" not in json.dumps(report) and "PRIVATE_DIAGNOSTIC" not in json.dumps(report),
                "self_test_settle_report_redaction")
        return actual, report, reads, ownership

    actual, report, reads, ownership = scenario([frame, frame, complete])
    stats = report["native_settle"]
    require(actual is complete and stats["ready"] and not stats["deadline_reached"]
            and stats["attempts"] == stats["completed_reads"] == stats["owner_checks"] == 3
            and len(reads) == len(ownership) == 3 and stats["elapsed_ms"] == 620.0
            and stats["max_read_ms"] == 30.0 and stats["first_nodes"] == 1
            and stats["first_marker_count"] == 1 and report["native_text"]["required_markers_present"] == len(MARKERS),
            "self_test_delayed_native_publication")

    actual, report, reads, ownership = scenario([frame], expected="native_practice_question_unreadable")
    require(actual is None and report["native_settle"]["deadline_reached"]
            and not report["native_settle"]["ready"] and report["native_settle"]["elapsed_ms"] == 1000.0
            and len(reads) == len(ownership) == 4 and report["native_text"]["nodes"] == 1
            and not report["native_text"]["question_markers_present"], "self_test_persistent_native_frame")

    for changed, failure in ((dict(complete, window_id='[":1.77","/different/frame"]'), "native_capture_scope_mismatch"),
                             (dict(complete, pid=13), "native_capture_scope_mismatch"),
                             (dict(complete, source="dom"), "native_capture_invalid")):
        _actual, report, reads, _ownership = scenario([frame, changed, complete], expected=failure)
        require(len(reads) == 2 and not report["native_settle"]["ready"], "self_test_native_scope_drift")

    for owners in ((11, 13), (11, 99)):
        _actual, report, reads, ownership = scenario([frame, complete], owners=owners,
                                                    expected="native_capture_scope_mismatch")
        require(len(reads) == 1 and len(ownership) == 2 and not report["native_settle"]["ready"],
                "self_test_native_owner_change")

    _actual, report, reads, _ownership = scenario([complete], read_delay=1.1,
                                                  expected="native_practice_question_unreadable")
    require(len(reads) == 1 and report["native_text"]["question_markers_present"]
            and report["native_settle"]["deadline_reached"] and not report["native_settle"]["ready"],
            "self_test_late_native_answer_rejected")

    for failure, code in ((subprocess.TimeoutExpired(["PRIVATE_DIAGNOSTIC"], .5), "native_practice_question_read_timeout"),
                          (CheckFailure("native_helper_exit_1"), "native_helper_exit_1")):
        _actual, report, reads, _ownership = scenario([failure, complete], expected=code)
        require(len(reads) == 1 and report["native_settle"]["completed_reads"] == 0
                and not report["native_settle"]["ready"], "self_test_native_helper_failure_not_retried")

    _actual, _report, reads, ownership = scenario([complete], source_env={}, expected="native_settle_target_mismatch")
    require(not reads and not ownership, "self_test_unpinned_native_read")

    completed = subprocess.CompletedProcess([], 0, json.dumps(complete).encode())
    with mock.patch("subprocess.run", return_value=completed) as run:
        require(read_native_tree(env, timeout=.125) == complete, "self_test_native_helper_result")
        run.assert_called_once_with([browser.SYSTEM_PYTHON, str(browser.HELPER), "--mode", "tree"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=.125)
    for completed, expected in ((subprocess.CompletedProcess([], 2, b"PRIVATE_DIAGNOSTIC"), "native_helper_exit_2"),
                                (subprocess.CompletedProcess([], 0, b"x" * 1_048_577), "native_helper_output_too_large")):
        with mock.patch("subprocess.run", return_value=completed):
            try:
                read_native_tree(env, timeout=.125)
            except CheckFailure as error:
                require(str(error) == expected, "self_test_native_helper_guard_failure")
            else:
                raise CheckFailure("self_test_native_helper_guard_missing")
    return 9


def self_test():
    from unittest import mock
    from types import SimpleNamespace
    require(has_markers("1. Two Sum\nnums = [2, 7, 11, 15], target = 9; return indices. "
                        "You may not use the same element twice."), "marker_spacing_regression")
    require(not has_markers("Two Sum target=9"), "marker_missing_input_guard")
    settle_guard_groups = self_test_native_settle()
    uid = os.getuid()
    records = {10: {"pid": 10, "uid": uid, "ppid": 1, "start": 20},
               11: {"pid": 11, "uid": uid, "ppid": 10, "start": 21},
               12: {"pid": 12, "uid": uid + 1, "ppid": 10, "start": 22}}
    require(is_owned_descendant(11, records[10], read=records.get), "descendant_guard")
    require(not is_owned_descendant(12, records[10], read=records.get), "foreign_uid_guard")
    require(not is_owned_descendant(11, dict(records[10], start=99), read=records.get), "pid_reuse_guard")
    audit = LiveAudit()
    audit.observe({"type": "answer_started", "request_id": "test"})
    audit.observe({"type": "answer_delta", "request_id": "test", "text": "real test stream"})
    audit.observe({"type": "answer_completed", "request_id": "test"})
    require(audit.completed and audit.answer == "real test stream", "event_observation_guard")
    for event in ({"type": "answer_started", "request_id": "second"},
                  {"type": "answer_delta", "request_id": "different", "text": "bad"}):
        try:
            audit.observe(event)
        except CheckFailure:
            pass
        else:
            raise CheckFailure("event_identity_guard_missing")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "owned-cli.json"
        path.write_text(json.dumps(records[10]))
        path.chmod(0o600)
        with mock.patch(__name__ + ".process_identity", return_value=dict(records[10], start=99)), mock.patch("os.killpg") as kill:
            stop_registered(root)
            require(not kill.called, "watchdog_pid_reuse_guard")
    with tempfile.TemporaryDirectory() as temporary:
        args = SimpleNamespace(backend="x11", runtime_dir=Path("/run/user") / str(uid),
                               session_bus_address="unix:path=/synthetic/bus", display=":99",
                               wayland_socket=Path("/synthetic/wayland-0"), xauthority=None,
                               cli=Path("/synthetic/cli"))
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_API_KEY": "SYNTHETIC_SECRET",
                                         "HTTPS_PROXY": "http://synthetic.invalid",
                                         "LD_LIBRARY_PATH": "/synthetic/private"}):
            isolated = environment(args, Path(temporary))
        require(not any(name in isolated for name in ("SOCKET_STREAM_API_KEY", "HTTPS_PROXY", "LD_LIBRARY_PATH")),
                "credential_environment_allowlist")
        require(isolated["DBUS_SESSION_BUS_ADDRESS"] == args.session_bus_address
                and isolated["SOCKET_STREAM_CLI"] == str(args.cli), "explicit_desktop_environment")
    metadata = page_probe({"url": "https://unapproved.invalid/private", "title": "PRIVATE_TITLE",
                           "text": "PRIVATE_TEXT", "ready": "complete"})
    require("PRIVATE" not in json.dumps(metadata) and not metadata["expected_url"], "page_metadata_redaction")
    pending = LiveAudit()
    pending.session_started_at = 1
    provider = SimpleNamespace(requests=[], lock=threading.Lock(), failure=None)
    require(needs_manual_capture(pending, provider, False, 9), "initial_manual_capture_missing")
    with provider.lock:
        require(not needs_manual_capture(pending, provider, False, 9), "inflight_provider_duplicate_capture")
    provider.requests.append({})
    require(not needs_manual_capture(pending, provider, False, 9), "observed_provider_duplicate_capture")
    provider.requests.clear()
    provider.failure = "synthetic_failure"
    require(not needs_manual_capture(pending, provider, False, 9), "failed_provider_manual_retry")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            register_owned(root, "chrome", child)
            (root / "owned-cli.json").symlink_to("/dev/null")
            try:
                stop_registered(root)
            except CheckFailure as error:
                require(str(error) == "owned_watchdog_cleanup_failed", "watchdog_guard_error")
            else:
                raise CheckFailure("registry_symlink_not_rejected")
            child.wait(timeout=2)
            require(child.returncode is not None, "independent_cleanup_did_not_reap")
        finally:
            browser.stop_owned(child)
    previous = set_subreaper()
    leader = None
    try:
        leader = subprocess.Popen([sys.executable, "-c",
            "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "print(child.pid,flush=True);sys.stdin.readline()"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        child_pid = int(leader.stdout.readline())
        leader.stdin.write("exit\n")
        leader.stdin.flush()
        leader.wait(timeout=2)
        require(process_identity(child_pid)["ppid"] == os.getpid(), "orphan_not_adopted")
        cleanup_children()
        require(process_identity(child_pid) is None, "exited_group_leader_descendant_survived")
    finally:
        if leader is not None:
            browser.stop_owned(leader)
            leader.stdin.close()
            leader.stdout.close()
        cleanup_children()
        set_subreaper(previous)
    emit({"schema": SCHEMA, "self_test": True, "passed": True, "guard_groups": 14 + settle_guard_groups,
          "native_settle_guard_groups": settle_guard_groups,
          "native_validation": False, "external_inference": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--private-root", help=argparse.SUPPRESS)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--session-bus-address")
    parser.add_argument("--wayland-socket", type=Path)
    parser.add_argument("--backend", choices=("x11", "wayland"), default="x11")
    parser.add_argument("--display")
    parser.add_argument("--xauthority", type=Path)
    parser.add_argument("--relay-dir", type=Path)
    parser.add_argument("--force-renderer-accessibility", action="store_true")
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=200)
    parser.add_argument("--hold-seconds", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        validate_args(args)
        if args.worker:
            return worker(args)
        with tempfile.TemporaryDirectory(prefix="floaty-public-gtk-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            child_env = environment(args, root)
            command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:],
                       "--worker", "--private-root", str(root)]
            previous = set_subreaper()
            try:
                streams, status, error = panel_check.collect_worker(command, child_env, args.timeout)
            finally:
                try:
                    stop_registered(root)
                finally:
                    try:
                        cleanup_children()
                    finally:
                        set_subreaper(previous)
            report = None
            phases = []
            for line in streams["stdout"].decode("utf-8", "replace").splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if value.get("kind") == "phase" and value.get("phase") in PHASES:
                    phases.append(value["phase"])
                if value.get("kind") == "result" and value.get("report", {}).get("schema") == SCHEMA:
                    report = value["report"]
            report = report or {"schema": SCHEMA, "passed": False, "failure_code": error or "worker_missing_report"}
            if error or status:
                report["passed"] = False
                report.setdefault("failure_code", error or "worker_failed")
            report["last_phase"] = phases[-1] if phases else "before_worker_start"
            report["parent_descendant_cleanup_completed"] = True
            emit(report)
            return 0 if report["passed"] else 1
    except Exception as error:
        code = str(error) if isinstance(error, CheckFailure) else type(error).__name__
        emit({"schema": SCHEMA, "passed": False, "failure_code": code})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
