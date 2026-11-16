#!/usr/bin/env python3
"""Opt-in real GTK3 checks; run on Linux under Xvfb or a desktop session.

    xvfb-run -a -s '-screen 0 1920x1080x24' python3 tests/gtk_workspace_checks.py

All questions/processes are synthetic. Preferences use a temporary directory.
SOCKET_STREAM_GTK_ARTIFACT_DIR optionally retains drawings of these test windows.
This checks the native UI, not received screen-share pixels or audio hardware.
"""
import ctypes
import ctypes.util
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps/socket_stream_desktop/ui"
sys.path.insert(0, str(UI))

from overlay_guard import detect_session, ensure_panel_backend

ensure_panel_backend(detect_session())
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

import ui_theme

spec = importlib.util.spec_from_file_location("gtk_workspace", UI / "linux-gtk.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def pump(milliseconds=180):
    loop = GLib.MainLoop()
    GLib.timeout_add(milliseconds, lambda: (loop.quit(), False)[1])
    loop.run()


def stamp():
    return time.strftime("%H:%M:%S.%f")[:-3]


def wait_for(predicate, timeout=4000):
    loop = GLib.MainLoop()
    reached = []

    def check():
        if predicate():
            reached.append(True)
            loop.quit()
            return False
        return True

    timer = GLib.timeout_add(10, check)
    deadline = GLib.timeout_add(timeout, lambda: (loop.quit(), False)[1])
    loop.run()
    if reached:
        GLib.source_remove(deadline)
    else:
        GLib.source_remove(timer)
    return bool(reached)


def text_of(widget):
    buffer = widget.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


def artifact(widget, name):
    directory = os.environ.get("SOCKET_STREAM_GTK_ARTIFACT_DIR")
    if not directory:
        return
    import cairo

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, widget.get_allocated_width(), widget.get_allocated_height())
    widget.draw(cairo.Context(surface))
    surface.write_to_png(str(path / (name + ".png")))


SOLUTION = (
    "Question: How can you solve Two Sum in O(n) time?\nAnswer: "
    "Use a **hash map** to remember the indices of numbers you've already seen.\n\n"
    "### Solution\n```python\ndef two_sum(nums, target):\n    seen = {}\n"
    "    for i, value in enumerate(nums):\n        complement = target - value\n"
    "        if complement in seen:\n            return [seen[complement], i]\n"
    "        seen[value] = i\n    return []\n```\n"
    "Look up the complement **before** inserting the current value, so an index is never reused.\n\n"
    "**Time:** O(n) average · **Space:** O(n)\n\n"
    "Works with duplicates, negative values, and a solution using the first element."
)

FAKE_CLI = '''#!/usr/bin/python3
import json, signal, sys
sequence = 0
def emit(kind, **fields):
    global sequence
    sequence += 1
    print(json.dumps(dict(schema=1, type=kind, session_id="synthetic-process", sequence=sequence,
                          source="microphone", **fields)), flush=True)
def stop(*_):
    emit("answer_delta", request_id="two", text="LATE AFTER STOP")
    emit("session_stopped")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
emit("session_started")
emit("question_detected", request_id="one", text="First synthetic question?")
emit("answer_delta", request_id="one", text="First synthetic answer")
emit("answer_completed", request_id="one")
if sys.argv[1] == "replay-events":
    emit("session_stopped")
    raise SystemExit(0)
for command in sys.stdin:
    if command.strip() == "pause":
        emit("session_paused")
    elif command.strip() == "resume":
        emit("session_resumed")
        emit("question_detected", request_id="two", text="Second synthetic question?")
        emit("answer_delta", request_id="two", text="Second synthetic answer")
'''


class WorkspaceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check()[0]:
            raise RuntimeError("GTK3 needs a display; use xvfb-run or your Linux desktop")
        cls.gtk_messages = []
        cls.callback_errors = []
        cls.log_handler = GLib.log_set_handler(
            "Gtk", GLib.LogLevelFlags.LEVEL_WARNING | GLib.LogLevelFlags.LEVEL_CRITICAL,
            lambda domain, level, message, data: cls.gtk_messages.append(message), None)
        cls.old_excepthook = sys.excepthook
        sys.excepthook = lambda kind, error, trace: cls.callback_errors.append(f"{kind.__name__}: {error}")
        cls.old_cli = controller.CLI

    @classmethod
    def tearDownClass(cls):
        sys.excepthook = cls.old_excepthook
        GLib.log_remove_handler("Gtk", cls.log_handler)
        controller.CLI = cls.old_cli

    def setUp(self):
        self.gtk_messages.clear()
        self.callback_errors.clear()
        self.directory = tempfile.TemporaryDirectory(prefix="socket-stream-gtk-")
        self.path = Path(self.directory.name)
        self.saved_env = {key: os.environ.get(key) for key in ("SOCKET_STREAM_CONFIG", "XDG_CONFIG_HOME")}
        os.environ["SOCKET_STREAM_CONFIG"] = str(self.path / "config.json")
        os.environ["XDG_CONFIG_HOME"] = str(self.path)
        fake = self.path / "synthetic-cli"
        fake.write_text(FAKE_CLI)
        fake.chmod(0o700)
        controller.CLI = fake
        self.app = controller.create_window()
        self.app.show_all()
        self.panel = self.app.floating
        self.panel.show()
        self.sequence = 0
        pump()

    def tearDown(self):
        self.app.on_exit()
        self.assertTrue(wait_for(lambda: getattr(self.panel, "_destroyed", False)), "owned child did not exit")
        self.assertEqual(self.callback_errors, [], "GTK callback exceptions")
        self.assertEqual(self.gtk_messages, [], "GTK warnings/criticals")
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()

    def send(self, kind, rid=None, **fields):
        self.sequence += 1
        event = dict(schema=1, type=kind, session_id="gtk-test", sequence=self.sequence, source="microphone", **fields)
        if rid is not None:
            event["request_id"] = rid
        self.app.apply_live_event(event)

    def seed(self, answer=SOLUTION):
        self.app.active_provider = "Synthetic preview"
        self.send("session_started")
        self.send("question_detected", "one", text="How can you solve Two Sum in O(n) time?")
        self.send("answer_delta", "one", text=answer)
        self.send("answer_completed", "one")
        pump()

    def test_theme_layout_and_compact_reading_space(self):
        provider = Gtk.CssProvider()
        errors = []
        provider.connect("parsing-error", lambda _p, _s, error: errors.append(str(error)))
        provider.load_from_data(ui_theme.CSS)
        self.assertEqual(errors, [])
        self.seed()
        self.panel.pin_question()
        pump()
        artifact(self.app, "workspace-session")
        artifact(self.panel.window, "answer-panel")
        for page in ("capture", "appearance", "shortcuts", "tools"):
            self.app.navigate(page)
            self.app.resize(840, 580)
            pump(200)
            self.assertEqual(self.app.workspace_stack.get_visible_child_name(), page)
            self.assertLessEqual(self.app.get_size().width, 1000, page)
            artifact(self.app, "workspace-" + page)
        self.panel.compact_button.set_active(True)
        pump(200)
        self.assertEqual(tuple(self.panel.window.get_size()), (560, 456))
        self.assertGreaterEqual(self.panel.answer_scroll.get_allocated_height(), 100)
        self.assertGreater(self.panel.answer_scroll.get_vadjustment().get_upper(),
                           self.panel.answer_scroll.get_vadjustment().get_page_size())
        self.assertEqual(self.panel.window.get_opacity(), 1.0)
        artifact(self.panel.window, "answer-compact")

    @unittest.skipUnless(os.environ.get("SOCKET_STREAM_GTK_POINTER_CHECKS") == "1", "opt-in pointer checks")
    def test_real_pointer_drag_resize_and_panel_identity(self):
        if not shutil.which("xdotool") or not shutil.which("xprop"):
            self.skipTest("xdotool and xprop are required")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11

        self.assertTrue(isinstance(self.panel.window.get_window(), GdkX11.X11Window))
        xid = self.panel.window.get_window().get_xid()
        properties = subprocess.check_output(["xprop", "-id", str(xid), "WM_CLASS", "_NET_WM_WINDOW_TYPE"], text=True)
        self.assertIn('"socket_stream_overlay"', properties)
        pointer = dict(line.split("=", 1) for line in subprocess.check_output(
            ["xdotool", "getmouselocation", "--shell"], text=True).splitlines() if "=" in line)
        from gtk_input import DesktopInput
        screen = self.app.get_screen()
        device = DesktopInput(screen.get_width(), screen.get_height())

        def move_drag(handle, dx, dy):
            self.panel.window.get_window().raise_()
            pump(60)
            wx, wy = self.panel.window.get_position()
            hx, hy = handle.translate_coordinates(self.panel.window, 0, 0)
            x = wx + hx + handle.get_allocated_width() // 2
            y = wy + hy + handle.get_allocated_height() // 2
            if os.environ.get("SOCKET_STREAM_DEBUG"):
                print(f"Pointer target: {x},{y}; XID={xid}; handle={hx},{hy}", flush=True)
            for action in (lambda: device.move(x, y), lambda: device.button(True),
                           lambda: device.move(x + dx, y + dy), lambda: device.button(False)):
                action()
                pump(90)
                if os.environ.get("SOCKET_STREAM_DEBUG"):
                    print(subprocess.check_output(["xdotool", "getmouselocation", "--shell"], text=True).strip(), flush=True)

        try:
            before = tuple(self.panel.window.get_position())
            if os.environ.get("SOCKET_STREAM_DEBUG"):
                print(f"[t] {stamp()} drag1-begin", flush=True)
            move_drag(self.panel.drag_handle, 64, 36)
            after = tuple(self.panel.window.get_position())
            self.assertEqual(after, (before[0] + 64, before[1] + 36))
            size = tuple(self.panel.window.get_size())
            if os.environ.get("SOCKET_STREAM_DEBUG"):
                print(f"[t] {stamp()} drag1-done drag2-begin", flush=True)
            move_drag(self.panel.resize_handle, 80, 60)
            self.assertEqual(tuple(self.panel.window.get_size()), (size[0] + 80, size[1] + 60))
            print(f"Pointer checks ({device.backend}): moved {before} -> {after}; resized {size} -> {tuple(self.panel.window.get_size())}; WM_CLASS=socket_stream_overlay", flush=True)
        finally:
            if os.environ.get("SOCKET_STREAM_DEBUG"):
                print(f"[t] {stamp()} drag-restore", flush=True)
            device.button(False)
            device.move(int(pointer["X"]), int(pointer["Y"]))
            device.close()

    @unittest.skipUnless(os.environ.get("SOCKET_STREAM_GTK_POINTER_CHECKS") == "1", "opt-in X11 key checks")
    def test_x11_meta_binding_receives_real_key_events(self):
        if not shutil.which("xdotool"):
            self.skipTest("xdotool is required")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11

        self.seed()
        self.app._install_shortcuts(controller.parse_bindings({"pin_question": "meta+f8"}), True)
        self.panel.hide()
        self.app.set_focus(None)
        self.app.present()
        pump()
        if os.environ.get("SOCKET_STREAM_DEBUG"):
            print(f"[t] {stamp()} key-test presented", flush=True)
        self.assertIsInstance(self.app.get_window(), GdkX11.X11Window)
        from gtk_input import DesktopInput
        screen = self.app.get_screen()
        device = DesktopInput(screen.get_width(), screen.get_height())
        pointer = dict(line.split("=", 1) for line in subprocess.check_output(
            ["xdotool", "getmouselocation", "--shell"], text=True).splitlines() if "=" in line)
        try:
            if device.fd is None:
                subprocess.run(["xdotool", "windowfocus", "--sync", str(self.app.get_window().get_xid())], check=True, timeout=3)
            else:
                # Window.get_position() includes WM/CSD frame offsets. Anchor
                # the click to the actual mapped client and its titlebar.
                header = self.app.get_titlebar()
                hx, hy = header.translate_coordinates(self.app, 0, 0)
                xid = self.app.get_window().get_xid()

                def pointer_report():
                    report = dict(line.split("=", 1) for line in subprocess.check_output(
                        ["xdotool", "getmouselocation", "--shell"], text=True).splitlines() if "=" in line)
                    return int(report["X"]), int(report["Y"]), report.get("WINDOW", "")

                def titlebar_target():
                    # Re-read every attempt: Mutter may still be placing the
                    # just-presented window, and a stale origin would aim the
                    # pointer beside the real titlebar.
                    _ok, x, y = self.app.get_window().get_origin()
                    return (x + hx + header.get_allocated_width() // 2,
                            y + hy + header.get_allocated_height() // 2)

                # Confirm arrival with this window's own enter/motion events:
                # their root coordinates are delivered by the compositor and
                # stay truthful even where xdotool's XQueryPointer report
                # freezes (observed on GNOME Wayland after another window's
                # drag). An event on this window also proves the pointer is
                # over it, so no separate window check is needed.
                arrivals = []

                def record_pointer(_widget, event):
                    arrivals.append((int(event.x_root), int(event.y_root)))
                    return False

                self.app.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
                self.app.connect("enter-notify-event", record_pointer)
                self.app.connect("motion-notify-event", record_pointer)

                def pointer_arrived(target):
                    if not arrivals:
                        return False
                    x, y = arrivals[-1]
                    return abs(x - target[0]) <= 12 and abs(y - target[1]) <= 12

                # The compositor only tracks pointer positions over X11
                # surfaces, so confirm the move landed on this window before
                # clicking: a click that lands on a Wayland-native surface
                # never focuses the control window.
                deadline = time.monotonic() + 3.0
                report = pointer_report()
                if os.environ.get("SOCKET_STREAM_DEBUG"):
                    print(f"[t] {stamp()} loop-begin", flush=True)
                while time.monotonic() < deadline:
                    target = titlebar_target()
                    if os.environ.get("SOCKET_STREAM_DEBUG"):
                        print(f"[t] {stamp()} Focus attempt: target={target}; XID={xid}; last event={arrivals[-1:]}", flush=True)
                    device.move(*target)
                    pump(70)
                    if pointer_arrived(target):
                        break
                    # Nudge off-target so the next approach posts fresh
                    # motion; identical absolute coordinates can be deduped.
                    device.move(target[0] + 6, target[1] + 6)
                    pump(40)
                else:
                    if os.environ.get("SOCKET_STREAM_DEBUG"):
                        state = self.app.get_window().get_state()
                        print(f"Failure state: visible={self.app.get_visible()} "
                              f"gdk_visible={self.app.get_window().is_visible()} state={state} "
                              f"origin={self.app.get_window().get_origin()[1:]}", flush=True)
                        for tool, argv in (
                            ("tree", ["xwininfo", "-root", "-tree"]),
                            ("active", ["xprop", "-root", "_NET_ACTIVE_WINDOW", "_NET_CLIENT_LIST_STACKING"]),
                            ("shape", ["xwininfo", "-id", str(xid), "-shape"]),
                        ):
                            try:
                                print(f"[diag:{tool}] " + subprocess.check_output(argv, text=True, timeout=5).strip(), flush=True)
                            except Exception as error:  # diagnostics only
                                print(f"[diag:{tool}] unavailable: {error}", flush=True)
                        probe = ctypes.CDLL(ctypes.util.find_library("X11"))
                        probe.XOpenDisplay.argtypes = [ctypes.c_char_p]
                        probe.XOpenDisplay.restype = ctypes.c_void_p
                        probe.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
                        probe.XDefaultRootWindow.restype = ctypes.c_ulong
                        probe.XGrabPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                                       ctypes.c_long, ctypes.c_int, ctypes.c_int,
                                                       ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
                        probe.XGrabPointer.restype = ctypes.c_int
                        probe.XUngrabPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                        probe.XCloseDisplay.argtypes = [ctypes.c_void_p]
                        pdpy = probe.XOpenDisplay(None)
                        if pdpy:
                            root = probe.XDefaultRootWindow(pdpy)
                            status = probe.XGrabPointer(pdpy, root, False, 0, 1, 1, 0, 0, 0)
                            probe.XUngrabPointer(pdpy, 0)
                            probe.XCloseDisplay(pdpy)
                            print(f"[diag:grab] XGrabPointer status={status} (0=free, 1=AlreadyGrabbed)", flush=True)
                        # Sweep fixed points: if any X11 surface answers, the
                        # report moves; a frozen report means the pointer is
                        # over Wayland-native surfaces everywhere or not moving.
                        for point in ((500, 400), (500, 54), (960, 540), (100, 900), (500, 54)):
                            device.move(*point)
                            pump(80)
                            print(f"[diag:sweep] {point} -> {pointer_report()}", flush=True)
                    self.fail(f"pointer never reached the control window titlebar: report={report} last_events={arrivals[-3:]}")
                if os.environ.get("SOCKET_STREAM_DEBUG"):
                    print(f"[t] {stamp()} Focus confirmed: target={target}; XID={xid}; pointer={report} event={arrivals[-1:]}", flush=True)
                device.button(True)
                pump(60)
                device.button(False)
            self.assertTrue(wait_for(self.app.has_toplevel_focus, timeout=1200), "control window did not receive desktop focus")
            for key, pressed in (("super", True), ("f8", True), ("f8", False), ("super", False)):
                device.key(key, pressed)
                pump(60)
            self.assertTrue(self.app.live_state.display_snapshot()["pinned"], "Meta/Super shortcut did not dispatch")
            print(f"Meta/Super + F8 dispatched through {device.backend}", flush=True)
        finally:
            device.move(int(pointer["X"]), int(pointer["Y"]))
            device.close()

    def test_shared_pinned_pair_rendering_history_and_copy(self):
        self.seed()
        self.assertIs(self.app.live_state, self.panel._model.state)
        self.panel.pin_button.clicked()
        self.send("question_detected", "two", text="What is binary search?")
        self.send("answer_delta", "two", text="Second answer")
        self.send("transcript_updated", text="New speech while the first answer stays pinned")
        pump()
        self.assertIn("two_sum", text_of(self.panel.answer))
        self.assertNotIn("```", text_of(self.panel.answer))
        self.assertNotIn("Question:", text_of(self.panel.answer))
        self.assertEqual(self.panel.question.get_text(), "How can you solve Two Sum in O(n) time?")
        self.assertEqual(self.panel.latest_button.get_label(), "1 new · Latest")
        self.assertEqual(len(self.app.session_questions.get_children()), 2)
        self.assertEqual(len(self.panel.history_list.get_children()), 2)
        self.assertEqual(self.app.live_state.snapshot()["answer"], "Second answer")
        self.assertIn("New speech", text_of(self.panel.transcript))
        artifact(self.panel.window, "answer-pinned-new-question")
        self.panel.history_button.set_active(True)
        pump()
        artifact(self.panel.window, "answer-history")
        self.panel.history_stack.set_visible_child_name("transcript")
        pump()
        artifact(self.panel.window, "answer-transcript")
        self.panel.history_button.set_active(False)
        copied = []
        self.panel._on_copy = copied.append
        self.panel.copy_button.clicked()
        self.assertIn("```python", copied[0])
        self.assertNotIn("Question:", copied[0])
        self.panel.latest_button.clicked()
        pump()
        self.assertEqual(text_of(self.panel.answer), "Second answer")
        self.panel.history_list.emit("row-activated", self.panel.history_list.get_row_at_index(1))
        pump()
        self.assertIn("two_sum", text_of(self.panel.answer))
        self.assertTrue(self.app.live_state.display_snapshot()["pinned"])

    def test_appearance_preview_save_and_reduced_motion(self):
        config = self.path / "config.json"
        self.app.navigate("appearance")
        self.app.opacity_scale.set_value(72)
        self.app.font_size.set_value(20)
        self.app.reduced_motion.set_active(True)
        self.assertFalse(config.exists(), "preview must not write preferences")
        self.assertEqual(self.panel.opacity, 0.72)
        self.assertEqual(self.panel.font_size, 20)
        self.assertFalse(self.panel._motion_enabled)
        self.assertEqual(self.panel.answer_stack.get_transition_duration(), 0)
        self.assertEqual(self.app.workspace_stack.get_transition_duration(), 0)
        self.panel.hide()
        self.panel.show()
        self.assertIsNone(self.panel._fade_tick)
        self.app.save_appearance()
        saved = json.loads(config.read_text())["desktop"]["appearance"]
        self.assertEqual(saved, dict(opacity=0.72, font_size=20, reduced_motion=True))
        settings = Gtk.Settings.get_default()
        old = settings.get_property("gtk-enable-animations")
        try:
            settings.set_property("gtk-enable-animations", False)
            self.app.reduced_motion.set_active(False)
            self.assertFalse(self.panel._motion_enabled)
            self.assertEqual(self.app.workspace_stack.get_transition_duration(), 0)
        finally:
            settings.set_property("gtk-enable-animations", old)

    def test_long_stream_preserves_reader_scroll_and_code_horizontal_access(self):
        self.seed("A long explanation. " * 600)
        adjustment = self.panel.answer_scroll.get_vadjustment()
        adjustment.set_value(200)
        pump()
        self.panel.append_answer(" More explanation." * 200)
        pump()
        self.assertAlmostEqual(adjustment.get_value(), 200, delta=2)
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        self.panel.append_answer("\n\nOne more paragraph. " * 20)
        pump()
        self.assertAlmostEqual(adjustment.get_value(), adjustment.get_upper() - adjustment.get_page_size(), delta=2)
        self.panel.set_answer("```python\nlong_variable = " + repr("x" * 150) + "\n```")
        pump()
        horizontal = self.panel.answer_scroll.get_hadjustment()
        self.assertGreater(horizontal.get_upper(), horizontal.get_page_size())
        self.assertIn("x" * 150, text_of(self.panel.answer))

    def test_shortcut_recording_conflicts_save_reset_and_dispatch(self):
        self.seed()
        self.app.navigate("shortcuts")
        self.app.record_shortcut("pin_question")
        modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        self.assertFalse(Gtk.accel_groups_activate(self.app, Gdk.KEY_q, modifiers))
        self.assertFalse(self.app._exiting, "recording Quit must not execute it")
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state, event.hardware_keycode, event.group = Gdk.KEY_q, modifiers, 0, 0
        entry = self.app.shortcut_entries["pin_question"]
        self.assertTrue(self.app.record_shortcut_key(entry, event, "pin_question"))
        self.app.save_shortcuts()
        self.assertFalse((self.path / "config.json").exists(), "duplicate must not be saved")
        self.assertIn("Could not save", self.app.shortcut_message.get_text())
        entry.set_text("Ctrl+Alt+F8")
        self.app.save_shortcuts()
        self.app.navigate("session")
        self.app.set_focus(None)
        self.assertTrue(Gtk.accel_groups_activate(self.app, Gdk.KEY_F8, modifiers))
        self.assertTrue(self.app.live_state.display_snapshot()["pinned"])
        self.app.reset_shortcuts()
        self.assertEqual(self.app.shortcuts.accelerator_for("pin_question").key, "f8")
        self.app.save_shortcuts()
        self.assertEqual(self.app.shortcuts.accelerator_for("pin_question").key, "b")
        self.app.shortcut_enabled.set_active(False)
        self.app.save_shortcuts()
        self.assertFalse(Gtk.accel_groups_activate(self.app, Gdk.KEY_b, modifiers))

    def test_owned_live_process_pause_stop_and_new_state(self):
        self.assertTrue(self.app.run_cli(["observe-live"]))
        self.assertTrue(wait_for(lambda: self.app.live_state.snapshot()["status"] == "ready"))
        old = self.app.live_state
        old_process = self.app.proc
        self.assertIs(old, self.panel._model.state)
        self.panel.pin_question()
        self.app.toggle_pause()
        self.app.toggle_pause()
        self.assertTrue(wait_for(lambda: old.snapshot()["status"] == "paused"))
        self.assertEqual(self.app.control_pause_button.get_label(), "Resume")
        self.app.toggle_pause()
        self.assertTrue(wait_for(lambda: old.snapshot()["request_id"] == "two"))
        self.assertEqual(self.panel.answer_text(), "First synthetic answer")
        self.app.stop()
        self.assertTrue(self.app._events_closed)
        self.assertTrue(wait_for(lambda: self.app.proc is None))
        self.assertTrue(old_process.stdin.closed)
        self.assertTrue(old_process.stdout.closed)
        self.assertEqual(old.display_snapshot()["answer"], "First synthetic answer")
        self.assertNotIn("LATE", old.snapshot()["answer"])
        self.assertTrue(self.app.run_cli(["observe-live"]))
        self.assertIsNot(self.app.live_state, old)
        self.assertIs(self.app.live_state, self.panel._model.state)
        self.assertTrue(wait_for(lambda: self.app.live_state.snapshot()["status"] == "ready"))
        self.assertEqual(len(self.app.live_state.history()), 1)
        self.app.on_exit()
        self.assertTrue(wait_for(lambda: getattr(self.panel, "_destroyed", False)))

    def test_stop_before_startup_and_finished_replay_keep_gate_closed(self):
        self.assertTrue(self.app.run_cli(["observe-live"]))
        state = self.app.live_state
        self.app.stop()
        self.assertEqual(state.snapshot()["status"], "stopped")
        self.assertTrue(wait_for(lambda: self.app.proc is None))
        self.assertEqual(state.history(), [])
        self.assertTrue(self.app.run_cli(["replay-events"]))
        self.assertTrue(wait_for(lambda: self.app.proc is None))
        self.assertEqual(self.app.live_state.display_snapshot()["answer"], "First synthetic answer")
        self.assertEqual(self.app.live_state.display_snapshot()["status"], "ready")
        self.assertEqual(self.app.live_state.snapshot()["status"], "stopped")

    def test_capture_setup_completion_after_exit_does_not_touch_widgets(self):
        started, release = threading.Event(), threading.Event()

        def install():
            started.set()
            if not release.wait(3):
                raise RuntimeError("synthetic setup was not released")
            return SimpleNamespace()

        self.app.capture_exclusion = SimpleNamespace(install=install, note=lambda: "")
        self.app._run_capture_action("install")
        try:
            self.assertTrue(wait_for(started.is_set))
            self.app.on_exit()
            self.assertTrue(wait_for(lambda: getattr(self.panel, "_destroyed", False)))
            self.app.refresh_capture_status = lambda: self.fail("late setup completion touched the closed UI")
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: not self.app.capture_busy))


if __name__ == "__main__":
    print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()} · {Gdk.Display.get_default().get_name()}", flush=True)
    unittest.main(verbosity=2)
