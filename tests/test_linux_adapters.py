"""Mocked Linux adapter tests; no GUI or live accessibility session."""
import importlib.util
import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCESS = load("access", "scripts/linux-accessibility.py")
GTK = load("gtk_shell", "apps/socket_stream_desktop/ui/linux-gtk.py")
ATSPI = SimpleNamespace(StateType=SimpleNamespace(ACTIVE="ACTIVE", FOCUSED="FOCUSED", PROTECTED="PROTECTED"))


class UnifiedStartTests(unittest.TestCase):
    def test_source_routing_and_consent_use_one_pipeline(self):
        build = GTK.live_start_args
        with self.assertRaisesRegex(ValueError, "Select audio"):
            build(False, False, "mock")
        self.assertEqual(build(False, True, "mock")[:2], ["observe-live", "--authorized"])
        audio = build(True, False, "codex", audio_source="system", speaker_role="interviewer")
        self.assertEqual(audio[:7], ["audio-live", "--authorized", "--source", "system",
                                     "--stt", "moonshine", "--speaker-role"])
        self.assertIn("interviewer", audio)
        combined = build(True, True, "mock")
        self.assertEqual(combined[0], "audio-live")
        self.assertIn("--read-screen", combined)
        self.assertNotIn("observe-live", combined)
        launches = []
        self.assertFalse(GTK.start_if_consented(True, False, False, audio, launches.append))
        self.assertEqual(launches, [])
        self.assertTrue(GTK.start_if_consented(True, False, True, audio, lambda args: launches.append(args) or True))
        self.assertEqual(launches, [audio])

    def test_single_start_workspace_has_both_source_checkboxes_and_no_listen_button(self):
        source = (ROOT / "apps/socket_stream_desktop/ui/workspace_ui.py").read_text()
        self.assertIn('label="Listen to audio"', source)
        self.assertIn('label="Read the screen"', source)
        self.assertIn('("Start", app.start)', source)
        self.assertNotIn('("Listen to audio", app.listen_audio)', source)
        self.assertNotIn('("Start observation", app.start)', source)


class LiveHandoffTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "apps/socket_stream_desktop/ui/linux-gtk.py").read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "deliver")
        self.queued, self.delivered, self.stopped = [], [], []
        self.slots = threading.BoundedSemaphore(32)
        proc = SimpleNamespace(terminate=lambda: self.stopped.append(True))
        self.controller = SimpleNamespace(proc=proc, _process_generation=3,
                                          _events_closed=False, _exiting=False)
        scope = dict(pending_ui=self.slots, proc=proc, self=self.controller,
                     generation=3, live_mode=True,
                     GLib=SimpleNamespace(idle_add=lambda callback: self.queued.append(callback)))
        exec(compile(ast.Module(body=[function], type_ignores=[]), "live-handoff", "exec"), scope)
        self.send = scope["deliver"]

    def test_real_delivery_closure_bounds_callbacks_and_stops_on_saturation(self):
        for index in range(32):
            self.assertTrue(self.send(self.delivered.append, index))
        self.assertFalse(self.send(self.delivered.append, "overflow"))
        self.assertEqual(len(self.queued), 32)
        self.assertEqual(self.stopped, [True])
        self.assertFalse(self.queued.pop(0)())
        self.assertEqual(self.delivered, [0])
        self.assertTrue(self.send(self.delivered.append, "reused"))
        self.assertEqual(len(self.queued), 32)

    def test_stale_or_stopped_delivery_releases_its_slot_without_mutating_ui(self):
        for attribute, value in (("_events_closed", True), ("_exiting", True),
                                 ("_process_generation", 4), ("proc", object())):
            with self.subTest(attribute=attribute):
                previous = getattr(self.controller, attribute)
                self.assertTrue(self.send(self.delivered.append, "late event"))
                setattr(self.controller, attribute, value)
                self.assertFalse(self.queued.pop(0)())
                self.assertEqual(self.delivered, [])
                # Every stale callback must return its bounded ownership slot.
                acquired = [self.slots.acquire(blocking=False) for _ in range(32)]
                self.assertTrue(all(acquired))
                for _ in acquired:
                    self.slots.release()
                setattr(self.controller, attribute, previous)

    def test_callback_error_also_releases_its_slot(self):
        def fail():
            raise ValueError("synthetic callback error")

        self.assertTrue(self.send(fail))
        with self.assertRaisesRegex(ValueError, "synthetic"):
            self.queued.pop(0)()
        self.assertTrue(all(self.slots.acquire(blocking=False) for _ in range(32)))


import os


class Node:
    def __init__(self, role="application", text=None, states=(), kids=(), name="", pid=None):
        self.role, self.text, self.states, self.kids = role, text, set(states), list(kids)
        self.name, self.pid = name, pid

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: state in self.states)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_process_id(self):
        return self.pid

    def get_text_iface(self):
        if self.text is None:
            return None
        return SimpleNamespace(get_character_count=lambda: len(self.text),
                               get_text=lambda start, end: self.text[start:end])

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, index):
        return self.kids[index]


class LinuxAdapterTests(unittest.TestCase):
    def test_atspi_text_dispatch_avoids_accessible_interface_getter_collision(self):
        value = "Explain the time complexity of bubble sort."

        class Interface:
            def get_character_count(self):
                return len(value)

            def get_text(self):
                return self  # Accessible's same-named zero-argument getter

        class TextNode(Node):
            def get_text_iface(self):
                return Interface()

        reads = []

        def explicit_read(iface, start, end):
            reads.append(end)
            return value[start:end]

        atspi = SimpleNamespace(StateType=ATSPI.StateType, Text=SimpleNamespace(get_text=explicit_read))
        node = TextNode("text", states=["FOCUSED"])
        app = Node(states=["ACTIVE"], kids=[node])
        self.assertEqual(ACCESS.extract(Node(kids=[app]), atspi), value)
        self.assertEqual(ACCESS.safe_text(node, atspi, 7), value[:7])
        self.assertEqual(reads[-1], 7)
        tree = ACCESS.build_tree(app, atspi, 20, 10, 1000)
        self.assertTrue(any(item["text"] == value for item in tree["nodes"]))
        reads.clear()
        protected = TextNode("password entry", states=["PROTECTED"])
        ACCESS.build_tree(protected, atspi, 20, 10, 1000)
        self.assertEqual(reads, [], "protected fields must never invoke the text API")

    def test_active_app_only(self):
        other = Node(kids=[Node("text", "other app")])
        active = Node(states=["ACTIVE"], kids=[Node("text", "foreground")])
        self.assertEqual(ACCESS.extract(Node(kids=[other, active]), ATSPI), "foreground")

    def test_focused_descendant_fallback(self):
        other = Node(kids=[Node("text", "other")])
        focus = Node(kids=[Node("text", "focused", states=["FOCUSED"])])
        self.assertEqual(ACCESS.extract(Node(kids=[other, focus]), ATSPI), "focused")

    def test_active_frame_beats_stale_background_focus_and_excludes_other_windows(self):
        stale = Node(kids=[Node("window", states=["FOCUSED"], kids=[Node("text", "background private text")])])
        background = Node("frame", kids=[Node("text", "unrelated background question")])
        active = Node("frame", states=["ACTIVE"], kids=[Node("text", "the actual question")])
        browser = Node(kids=[background, active])
        self.assertEqual(ACCESS.extract(Node(kids=[stale, browser]), ATSPI), "the actual question")

    def test_ambiguous_active_frames_require_focus_instead_of_reading_background(self):
        left = Node(kids=[Node("frame", states=["ACTIVE"], kids=[Node("text", "left private")])])
        right = Node(kids=[Node("frame", states=["ACTIVE"], kids=[Node("text", "right private")])])
        with self.assertRaisesRegex(RuntimeError, "Multiple accessible windows"):
            ACCESS.select_foreground(Node(kids=[left, right]), ATSPI)
        right.kids[0].kids[0].states.add("FOCUSED")
        self.assertIs(ACCESS.select_foreground(Node(kids=[left, right]), ATSPI), right.kids[0])

    def test_own_active_frame_is_never_captured_even_with_generic_application_name(self):
        app = Node(name="python3", kids=[Node("frame", name="SocketStream — Practice Answer", states=["ACTIVE"])])
        with self.assertRaises(ACCESS.OwnWindowError):
            ACCESS.select_foreground(Node(kids=[app]), ATSPI)

    def test_non_active_focus_does_not_override_ambiguous_active_windows(self):
        stale = [Node(kids=[Node("frame", states=["ACTIVE"], kids=[Node("text", "background")])]) for _ in range(2)]
        focused = Node("window", kids=[Node("text", "actual question", states=["FOCUSED"])])
        desktop = Node(kids=[*stale, Node(kids=[focused])])
        with self.assertRaisesRegex(RuntimeError, "Multiple accessible windows"):
            ACCESS.select_foreground(desktop, ATSPI)

    def test_explicit_window_is_scoped_and_never_retargeted(self):
        chosen = Node("frame", kids=[Node("text", "chosen question")])
        chosen.app, chosen.path = SimpleNamespace(bus_name=":1.99"), "/window/1"
        other = Node("frame", states=["ACTIVE"], kids=[Node("text", "private background")])
        desktop = Node(kids=[Node(kids=[chosen, other])])
        from unittest.mock import patch
        with patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": ACCESS.window_identity(chosen)}):
            self.assertEqual(ACCESS.extract(desktop, ATSPI), "chosen question")
            desktop.kids[0].kids.remove(chosen)
            with self.assertRaisesRegex(RuntimeError, "Selected question window is unavailable"):
                ACCESS.extract(desktop, ATSPI)
        with patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": "malformed"}):
            with self.assertRaisesRegex(RuntimeError, "Selected question window is unavailable"):
                ACCESS.extract(desktop, ATSPI)
        own = Node("frame", name="SocketStream — Practice Answer")
        own.app, own.path = SimpleNamespace(bus_name=":1.97"), "/window/3"
        with patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": ACCESS.window_identity(own)}):
            with self.assertRaises(ACCESS.OwnWindowError):
                ACCESS.extract(Node(kids=[Node(kids=[own])]), ATSPI)

    def test_window_inventory_does_not_read_descendant_text(self):
        frame = Node("frame", name="Practice fixture")
        frame.app, frame.path = SimpleNamespace(bus_name=":1.98"), "/window/2"
        frame.get_text_iface = lambda: self.fail("inventory read text")
        frame.get_child_count = lambda: self.fail("inventory traversed descendants")
        self.assertEqual(ACCESS.list_windows(Node(kids=[Node(kids=[frame])]), ATSPI),
                         [{"id": ACCESS.window_identity(frame), "title": "Practice fixture", "application": ""}])
        self.assertEqual(ACCESS.list_windows(Node(kids=[Node(name="gnome-shell", kids=[frame])]), ATSPI), [])
        self.assertEqual(ACCESS.list_windows(Node(kids=[Node(name="mutter-x11-frames", kids=[frame])]), ATSPI), [])

    def test_active_application_fallback_reads_only_its_focused_frame(self):
        background = Node("frame", kids=[Node("text", "background private text")])
        current = Node("frame", kids=[Node("text", "current question", states=["FOCUSED"])])
        app = Node(states=["ACTIVE"], kids=[background, current])
        self.assertEqual(ACCESS.extract(Node(kids=[app]), ATSPI), "current question")
        app.states.clear()
        self.assertEqual(ACCESS.extract(Node(kids=[app]), ATSPI), "current question")
        current.kids[0].states.clear()
        app.states.add("ACTIVE")
        with self.assertRaisesRegex(RuntimeError, "Multiple application windows"):
            ACCESS.extract(Node(kids=[app]), ATSPI)

    def test_no_foreground_returns_empty(self):
        self.assertEqual(ACCESS.extract(Node(kids=[Node(kids=[Node("text", "secret")])]), ATSPI), "")

    def test_own_gui_is_not_treated_as_question(self):
        active = Node(states=["ACTIVE"], name="SocketStream", kids=[Node("text", "generated answer")])
        with self.assertRaises(ACCESS.OwnWindowError):
            ACCESS.extract(Node(kids=[active]), ATSPI)

    def test_password_and_protected_descendants_excluded(self):
        password = Node("password entry", "secret", kids=[Node("text", "nested")])
        protected = Node("entry", "pin", states=["PROTECTED"])
        active = Node(states=["ACTIVE"], kids=[password, protected, Node("text", "safe")])
        self.assertEqual(ACCESS.extract(Node(kids=[active]), ATSPI), "safe")

    def test_deduplicates_bounds_and_handles_cycle(self):
        active = Node(states=["ACTIVE"])
        active.kids = [Node("text", "same"), Node("text", "same"), Node("text", "x" * 100000), active]
        result = ACCESS.extract(Node(kids=[active]), ATSPI)
        self.assertEqual(result.count("same"), 1)
        self.assertLessEqual(sum(map(len, result.split("\n"))), ACCESS.MAX_CHARS)

    def test_gtk_import_is_headless(self):
        self.assertTrue(callable(GTK.main))

    def test_observe_args_require_supported_provider(self):
        for provider in ("mock", "codex", "openai-compatible", "ollama"):
            args = GTK.observe_args(provider)
            self.assertIn("--authorized", args)
            self.assertEqual(args[-1], provider)
        with self.assertRaises(ValueError):
            GTK.observe_args("unknown")

    def test_status_tracks_real_process(self):
        running = SimpleNamespace(poll=lambda: None)
        exited = SimpleNamespace(poll=lambda: 1)
        self.assertEqual(GTK.capture_status(None), "Stopped")
        self.assertEqual(GTK.capture_status(running), "Active")
        self.assertEqual(GTK.capture_status(running, True), "Stopping")
        self.assertEqual(GTK.capture_status(exited), "Stopped")

    def test_floating_panel_compact_mode_keeps_normal_visible_window_sizes(self):
        self.assertEqual(GTK.floating_panel_size(False), (720, 620))
        self.assertEqual(GTK.floating_panel_size(True), (560, 456))

    def test_minimize_iconifies_main_window_without_touching_panel(self):
        class Window:
            iconified = False

            def iconify(self):
                self.iconified = True

        window = Window()
        GTK.minimize_app_window(window)
        self.assertTrue(window.iconified)

    def test_observation_interval_validation(self):
        self.assertEqual(GTK.observe_args("mock", 7)[3], "7")
        for interval in (0, 1, 3601):
            with self.assertRaises(ValueError):
                GTK.observe_args("mock", interval)

    def test_linux_meta_alias_uses_the_super_modifier(self):
        gdk = SimpleNamespace(ModifierType=SimpleNamespace(
            CONTROL_MASK=4, MOD1_MASK=8, SHIFT_MASK=1, SUPER_MASK=1 << 26, META_MASK=1 << 28))
        mask = GTK.gtk_modifier_mask(gdk, GTK.Accelerator.parse("ctrl+meta+f8"))
        self.assertEqual(mask, 4 | (1 << 26))

    def test_stop_only_terminates_owned_process_group(self):
        from unittest import mock
        proc = SimpleNamespace(pid=42699, poll=lambda: None)
        with mock.patch.object(GTK.os, "killpg") as killpg:
            self.assertTrue(GTK.terminate_tree(proc))
            killpg.assert_called_once_with(42699, GTK.signal.SIGTERM)
        proc_finished = SimpleNamespace(pid=42700, poll=lambda: 0)
        with mock.patch.object(GTK.os, "killpg") as killpg:
            self.assertFalse(GTK.terminate_tree(proc_finished))
            killpg.assert_not_called()

    def test_linux_portable_launcher_files_are_present(self):
        for path in (
            ROOT / "scripts/linux-portable-launch.sh",
            ROOT / "scripts/build-linux-release.sh",
            ROOT / "scripts/linux-check-dependencies.sh",
            ROOT / "docs/LINUX-TESTING.md",
        ):
            self.assertTrue(path.is_file(), str(path))


class ManualQuestionDispatch(unittest.TestCase):
    """Panel question/capture dispatch: stdin controls when live, ask otherwise."""

    @classmethod
    def _method(cls, name):
        tree = ast.parse((ROOT / "apps/socket_stream_desktop/ui/linux-gtk.py").read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == name)
        scope = {"sanitize_manual_question": GTK.sanitize_manual_question}
        exec(compile(ast.Module(body=[function], type_ignores=[]), name, "exec"), scope)
        return scope[name]

    def controller(self, proc=None, stdin_live=False):
        sent, launched, shown, messages, confirmed = [], [], [], [], []
        fake = SimpleNamespace(
            proc=proc, _stdin_live=stdin_live,
            question_window_target=None, question_window_title="",
            write=lambda text: messages.append(text),
            run_cli=lambda args: (launched.append(args) or True),
            selected_provider=lambda: "mock",
            show_answer_panel=lambda: shown.append(True),
            confirmation=lambda title, text: confirmed.append((title, text)) or self.confirm_result,
            set_status=lambda status: messages.append(f"status:{status}"),
        )
        return fake, SimpleNamespace(sent=sent, launched=launched, shown=shown,
                                     messages=messages, confirmed=confirmed)

    class RecordingStdin:
        def __init__(self, sink):
            self._sink = sink
        def write(self, line):
            self._sink.append(line)
        def flush(self):
            pass

    def setUp(self):
        self.confirm_result = True

    def test_sanitize_flattens_whitespace_and_bounds_the_question(self):
        self.assertEqual(GTK.sanitize_manual_question("  why\n\tdoes  this fail? "),
                         "why does this fail?")
        self.assertEqual(GTK.sanitize_manual_question("x" * 5000), "x" * 2000)
        self.assertEqual(GTK.sanitize_manual_question(None), "")
        self.assertEqual(GTK.sanitize_manual_question("   "), "")

    def test_live_session_sends_question_control_line(self):
        proc = SimpleNamespace(poll=lambda: None,
                               stdin=self.RecordingStdin((lines := [])))
        fake, seen = self.controller(proc=proc, stdin_live=True)
        self._method("send_manual_question")(fake, "why\n does  this fail?")
        self.assertEqual(lines, ["question why does this fail?\n"])
        self.assertEqual(seen.launched, [])

    def test_standalone_question_runs_one_shot_ask(self):
        fake, seen = self.controller()
        self._method("send_manual_question")(fake, "explain binary search")
        self.assertEqual(seen.launched,
                         [["ask", "--question", "explain binary search", "--provider", "mock"]])
        self.assertEqual(seen.shown, [True])

    def test_question_is_blocked_while_a_non_live_operation_runs(self):
        proc = SimpleNamespace(poll=lambda: None, stdin=None)
        fake, seen = self.controller(proc=proc, stdin_live=False)
        self._method("send_manual_question")(fake, "hello there")
        self.assertEqual(seen.launched, [])
        self.assertTrue(any("Stop the active operation" in text for text in seen.messages))

    def test_live_capture_sends_read_screen_control(self):
        proc = SimpleNamespace(poll=lambda: None,
                               stdin=self.RecordingStdin((lines := [])))
        fake, seen = self.controller(proc=proc, stdin_live=True)
        self._method("manual_capture")(fake)
        self.assertEqual(lines, ["read-screen\n"])
        self.assertEqual(seen.launched, [])

    def test_standalone_capture_confirms_then_runs_authorized_ask(self):
        fake, seen = self.controller()
        self._method("manual_capture")(fake)
        self.assertEqual(len(seen.confirmed), 1)
        self.assertEqual(seen.launched,
                         [["ask", "--read-screen", "--authorized", "--allow-ocr",
                           "--provider", "mock"]])
        self.assertEqual(seen.shown, [True])

    def test_standalone_capture_respects_declined_consent(self):
        self.confirm_result = False
        fake, seen = self.controller()
        self._method("manual_capture")(fake)
        self.assertEqual(seen.launched, [])
        self.assertIn("status:Permission declined", seen.messages)

    def test_selected_window_capture_consent_and_no_pixel_arguments(self):
        fake, seen = self.controller()
        fake.question_window_target = '[":1.99","/window/1"]'
        fake.question_window_title = "Practice fixture"
        self._method("manual_capture")(fake)
        self.assertIn("Practice fixture", seen.confirmed[0][1])
        self.assertIn("no screenshots", seen.confirmed[0][1])
        self.assertNotIn("--allow-ocr", seen.launched[0])
        self.assertIn("--no-vision", seen.launched[0])


if __name__ == "__main__":
    unittest.main()
