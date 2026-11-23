"""Tests for the floating overlay module (headless: no GTK required)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import floating_overlay as overlay  # noqa: E402


def event(kind, sequence, **fields):
    base = {
        "schema": 1, "type": kind, "session_id": "s1", "sequence": sequence,
        "source": "screen", "timestamp": "2026-11-03T00:00:00Z",
    }
    base.update(fields)
    return base


class ImportWithoutGtk(unittest.TestCase):
    def test_module_does_not_import_gtk(self):
        self.assertNotIn("gi", sys.modules)
        self.assertTrue(hasattr(overlay, "FloatingOverlay"))
        self.assertTrue(callable(overlay.main))

    def test_no_gtk_means_main_reports_cleanly(self):
        # On a machine without PyGObject, main() must fail gracefully, not crash.
        if "gi" in sys.modules:
            self.skipTest("GTK is installed on this machine")
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = overlay.main(["--stdin"])
        self.assertEqual(code, 2)
        self.assertIn("GTK3 unavailable", stderr.getvalue())


class ArgumentParsing(unittest.TestCase):
    def test_defaults(self):
        args = overlay.parse_args([])
        self.assertFalse(args.stdin)
        self.assertIsNone(args.provider)
        self.assertTrue(args.stealth)
        self.assertFalse(args.compact)
        self.assertEqual(args.interval, 4)

    def test_no_stealth_flag(self):
        self.assertFalse(overlay.parse_args(["--no-stealth"]).stealth)

    def test_stdin_and_provider(self):
        args = overlay.parse_args(["--stdin", "--provider", "ollama", "--compact"])
        self.assertTrue(args.stdin)
        self.assertEqual(args.provider, "ollama")
        self.assertTrue(args.compact)

    def test_default_panel_opacity_is_slightly_transparent(self):
        self.assertEqual(overlay.parse_args([]).opacity, 0.90)

    def test_panel_opacity_can_be_configured_within_subtle_range(self):
        self.assertEqual(overlay.parse_args(["--opacity", "0.9"]).opacity, 0.9)
        with self.assertRaises(SystemExit):
            overlay.parse_args(["--opacity", "0.5"])


class InputShapePlanning(unittest.TestCase):
    """Pure hover pass-through planner: no GTK, no display server."""

    def test_unlocked_keeps_only_the_given_input_areas(self):
        rects = overlay.input_region_rects(
            [(12, 8, 200, 36), (0, 580, 720, 40)], (720, 620), locked=False)
        self.assertEqual(rects, [(12, 8, 200, 36), (0, 580, 720, 40)])

    def test_locked_accepts_input_everywhere_again(self):
        rects = overlay.input_region_rects([(12, 8, 200, 36)], (720, 620), locked=True)
        self.assertEqual(rects, [(0, 0, 720, 620)])

    def test_rects_are_clamped_to_the_window_and_dropped_when_empty(self):
        rects = overlay.input_region_rects(
            [(-10, -10, 40, 40), (700, 600, 400, 400), (5, 5, 0, 10), (5, 5, 10, 0)],
            (720, 620), locked=False)
        self.assertEqual(rects, [(0, 0, 30, 30), (700, 600, 20, 20)])

    def test_degenerate_window_has_no_input_regions(self):
        self.assertEqual(overlay.input_region_rects([(0, 0, 10, 10)], (0, 400), False), [])
        self.assertEqual(overlay.input_region_rects([(0, 0, 10, 10)], (400, -1), False), [])

    def test_non_numeric_rects_are_dropped_without_raising(self):
        self.assertEqual(overlay.input_region_rects([("x", 0, 10, 10)], (100, 100), False), [])


class ConversationLayoutContract(unittest.TestCase):
    """Keep the native chat reader connected on Linux and macOS."""

    def test_linux_panel_uses_bounded_history_reader(self):
        source = (UI / "floating_overlay.py").read_text()
        self.assertIn("from conversation_reader import ConversationReader", source)
        self.assertIn("self.conversation.update(self._model.state.history()", source)
        reader = (UI / "conversation_reader.py").read_text()
        self.assertIn("Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)", reader)
        self.assertIn("reorder_child(question, 0)", reader)
        self.assertIn("def update(self, history", reader)

    def test_macos_panel_has_scrollable_vertical_question_answer_feed(self):
        source = (UI / "mac-app.swift").read_text()
        self.assertIn("answerScroll.documentView = answerView", source)
        self.assertIn("answerView.textContainer?.widthTracksTextView = true", source)
        self.assertIn(r'"\n\nAnswer\n"', source)
        self.assertIn("chatRanges.count > 24", source)

class PanelManualControls(unittest.TestCase):
    """The panel source must expose the manual prompt and pass-through hooks."""

    @classmethod
    def setUpClass(cls):
        import ast
        source = (ROOT / "apps" / "socket_stream_desktop" / "ui" / "floating_overlay.py").read_text()
        cls.tree = ast.parse(source)
        cls.class_node = next(node for node in ast.walk(cls.tree)
                              if isinstance(node, ast.ClassDef) and node.name == "FloatingOverlay")

    def test_constructor_accepts_ask_and_capture_callbacks(self):
        import ast
        init = next(node for node in self.class_node.body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        keywords = [arg.arg for arg in init.args.kwonlyargs]
        for required in ("on_ask", "on_capture"):
            self.assertIn(required, keywords)

    def test_pass_through_and_shape_methods_exist(self):
        import ast
        names = {node.name for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)}
        for required in ("_toggle_input_lock", "_apply_input_shape", "_collect_input_rects",
                         "_input_widget_list", "_on_prompt_activate", "_on_capture_clicked"):
            self.assertIn(required, names)


class ObserveCommand(unittest.TestCase):
    def test_base_command(self):
        command = overlay.observe_command("/bin/socket_stream", "ollama")
        self.assertEqual(command[:2], ["/bin/socket_stream", "observe-live"])
        self.assertIn("--authorized", command)
        self.assertIn("ollama", command)
        self.assertNotIn("--allow-ocr", command)
        self.assertNotIn("--portal", command)

    def test_optional_flags(self):
        command = overlay.observe_command("/bin/socket_stream", "mock", 9, True, True)
        self.assertIn("--allow-ocr", command)
        self.assertIn("--portal", command)
        self.assertEqual(command[command.index("--interval") + 1], "9")

    def test_default_cli_path_is_a_string(self):
        self.assertIsInstance(overlay.default_cli_path(), str)


class OverlayModel(unittest.TestCase):
    def test_feed_maps_events_to_view_state(self):
        model = overlay.OverlayModel()
        self.assertTrue(model.feed(event("session_started", 1)))
        self.assertTrue(model.feed(event(
            "question_detected", 2, request_id="q1",
            text="Find two numbers that sum to a target",
        )))
        self.assertTrue(model.feed(event(
            "answer_delta", 3, request_id="q1", text="Use a hash map.",
        )))
        self.assertTrue(model.feed(event("answer_completed", 4, request_id="q1")))
        state = model.snapshot()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["question"], "Find two numbers that sum to a target")
        self.assertEqual(state["answer"], "Use a hash map.")
        self.assertEqual(model.status_line("ollama"), "Ready · screen · ollama")

    def test_stale_answer_is_ignored(self):
        model = overlay.OverlayModel()
        model.feed(event("session_started", 1))
        model.feed(event("question_detected", 2, request_id="q1", text="First question here"))
        model.feed(event("question_detected", 3, request_id="q2", text="Second question here"))
        model.feed(event("answer_delta", 4, request_id="q1", text="stale"))
        self.assertEqual(model.snapshot()["answer"], "")

    def test_duplicate_request_keeps_answer_but_new_request_can_repeat_text(self):
        model = overlay.OverlayModel()
        model.feed(event("session_started", 1))
        model.feed(event("question_detected", 2, request_id="q1", text="A long enough question"))
        model.feed(event("answer_delta", 3, request_id="q1", text="answer"))
        model.feed(event("answer_completed", 4, request_id="q1"))
        self.assertFalse(model.feed(event(
            "question_detected", 5, request_id="q1", text="A long enough question",
        )))
        self.assertEqual(model.snapshot()["answer"], "answer")
        # A newly requested answer (including after Resume) must not be lost
        # merely because the recognized prompt is identical.
        self.assertTrue(model.feed(event(
            "question_detected", 6, request_id="q2", text="A long enough question",
        )))
        self.assertEqual(model.snapshot()["answer"], "")
        self.assertTrue(model.feed(event("answer_delta", 7, request_id="q2", text="fresh answer")))
        self.assertEqual(model.snapshot()["answer"], "fresh answer")


class VisibilityControls(unittest.TestCase):
    class Window:
        def __init__(self, visible=True):
            self.visible = visible

        def get_visible(self):
            return self.visible

        def hide(self):
            self.visible = False

        def show_all(self):
            self.visible = True

    def make_overlay(self, *, user_hidden=False, visible=True):
        panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
        panel._user_hidden = user_hidden
        panel._ever_shown = True
        panel._share_active = False
        panel.stealth = False
        panel.plan = None
        panel.window = self.Window(visible)
        return panel

    def test_explicit_hide_closes_panel_until_show_is_requested(self):
        panel = self.make_overlay()
        panel.hide()
        self.assertFalse(panel.window.get_visible())
        self.assertTrue(panel.show())
        self.assertTrue(panel.window.get_visible())

    def test_explicitly_hidden_panel_stays_hidden_during_share_state_updates(self):
        panel = self.make_overlay(user_hidden=True, visible=False)
        panel.apply_share_state(True)
        self.assertFalse(panel.window.get_visible())

    def test_toggle_never_closes_a_visible_panel(self):
        panel = self.make_overlay(visible=True)
        self.assertTrue(panel.toggle())
        self.assertTrue(panel.window.get_visible())

    def test_screen_share_does_not_hide_a_visible_panel(self):
        panel = self.make_overlay(visible=True)
        self.assertTrue(panel.apply_share_state(True))
        self.assertTrue(panel.window.get_visible())


class PanelIdentity(unittest.TestCase):
    class GLib:
        def __init__(self):
            self.name = "linux-gtk.py"

        def get_prgname(self):
            return self.name

        def set_prgname(self, value):
            self.name = value

    class Gdk:
        def __init__(self):
            self.program_class = "LinuxGtk"

        def get_program_class(self):
            return self.program_class

        def set_program_class(self, value):
            self.program_class = value

    def test_panel_identity_is_used_during_realize_and_restored(self):
        panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
        panel._GLib = self.GLib()
        panel._Gdk = self.Gdk()
        seen = {}

        def action():
            seen["name"] = panel._GLib.name
            seen["class"] = panel._Gdk.program_class
            return "realized"

        self.assertEqual(panel._with_panel_identity(action), "realized")
        self.assertEqual(seen["name"], overlay.PANEL_APP_ID)
        self.assertEqual(seen["class"], overlay.PANEL_WM_CLASS)
        self.assertEqual(panel._GLib.name, "linux-gtk.py")
        self.assertEqual(panel._Gdk.program_class, "LinuxGtk")

    def test_show_maps_with_panel_identity(self):
        panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
        panel._GLib = self.GLib()
        panel._Gdk = self.Gdk()
        panel._user_hidden = True
        panel._ever_shown = False
        seen = {}

        class Window:
            def show_all(_self):
                seen["name"] = panel._GLib.name
                seen["class"] = panel._Gdk.program_class

        panel.window = Window()
        panel._share_active = False
        panel.plan = type("Plan", (), {"guaranteed": False})()
        panel.is_visible = lambda: True
        panel._should_hide = lambda: False

        panel.show()

        # GTK3 Wayland reads g_get_prgname() at map time, so show() must
        # advertise the panel identity while the toplevel is first mapped.
        self.assertEqual(seen["name"], overlay.PANEL_APP_ID)
        self.assertEqual(seen["class"], overlay.PANEL_WM_CLASS)
        self.assertEqual(panel._GLib.name, "linux-gtk.py")
        self.assertEqual(panel._Gdk.program_class, "LinuxGtk")

    def test_identity_constants_match_capture_rules(self):
        import capture_rules

        self.assertEqual(overlay.PANEL_APP_ID, capture_rules.PANEL_APP_ID)
        self.assertEqual(overlay.PANEL_WM_CLASS, capture_rules.PANEL_WM_CLASS)


class OverrideRedirect(unittest.TestCase):
    """X11 override-redirect is a GdkWindow operation, not a GtkWindow one."""

    def _panel(self, window):
        panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
        panel.window = window
        hint = type("Hint", (), {"POPUP_MENU": "popup-menu", "NORMAL": "normal"})
        panel._Gdk = type("Gdk", (), {"WindowTypeHint": hint})
        return panel

    def test_gdk_window_override_redirect_is_used_and_centred(self):
        calls = []

        class GdkWindow:
            def set_override_redirect(self, value):
                calls.append(("override", value))

        class Window:
            def get_window(self):
                return GdkWindow()

            def get_mapped(self):
                return False

            def set_decorated(self, value):
                calls.append(("decorated", value))

        panel = self._panel(Window())
        panel._center_window = lambda: calls.append(("center", True))

        self.assertTrue(panel._try_override_redirect())
        self.assertEqual(
            calls,
            [("override", True), ("decorated", False), ("center", True)],
        )

    def test_refuses_to_change_a_mapped_window(self):
        class Window:
            def get_mapped(self):
                return True

            def get_window(self):
                raise AssertionError("a mapped window must not be touched")

        panel = self._panel(Window())
        self.assertFalse(panel._try_override_redirect())

    def test_falls_back_to_popup_menu_hint(self):
        calls = []

        class Window:
            def get_window(self):
                return None

            def get_mapped(self):
                return False

            def set_type_hint(self, hint):
                calls.append(("hint", hint))

            def set_decorated(self, value):
                calls.append(("decorated", value))

        panel = self._panel(Window())

        self.assertTrue(panel._try_override_redirect())
        self.assertEqual(calls, [("hint", "popup-menu"), ("decorated", False)])


class XwaylandDockHints(unittest.TestCase):
    def test_dock_plan_uses_the_dock_layer_without_decorations(self):
        calls = []

        class Window:
            def set_keep_above(self, value):
                calls.append(("keep_above", value))

            def set_skip_taskbar_hint(self, value):
                calls.append(("skip_taskbar", value))

            def set_skip_pager_hint(self, value):
                calls.append(("skip_pager", value))

            def set_type_hint(self, value):
                calls.append(("hint", value))

            def set_decorated(self, value):
                calls.append(("decorated", value))

        hint = type("Hint", (), {"DOCK": "dock", "NORMAL": "normal"})
        panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
        panel.window = Window()
        panel._Gdk = type("Gdk", (), {"WindowTypeHint": hint})
        panel.plan = overlay.build_plan(
            overlay.detect_session({
                "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
                "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            }),
            xwayland_dock=True,
        )
        panel.stealth = True
        panel._applied = []

        applied = panel._apply_plan()

        self.assertIn("type-hint=dock", applied)
        self.assertIn(("hint", "dock"), calls)
        self.assertIn(("decorated", False), calls)
        self.assertNotIn("override-redirect (unmanaged)", applied)


class Gtk3Pinning(unittest.TestCase):
    """Ubuntu 26.04 ships GTK4; an unpinned Gdk import resolves to 4.0."""

    def test_backend_is_chosen_before_gtk_import(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "apps/socket_stream_desktop/ui/floating_overlay.py",
            "apps/socket_stream_desktop/ui/linux-gtk.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertLess(
                source.index("ensure_panel_backend"),
                source.rindex("import gi"),
                f"{relative}: GDK backend must be chosen before GTK is imported",
            )

    def test_gtk3_entry_points_pin_gdk_3(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "apps/socket_stream_desktop/ui/floating_overlay.py",
            "apps/socket_stream_desktop/ui/linux-gtk.py",
            "security-lab/overlay_probe.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn('require_version("Gdk", "3.0")', source, relative)
            self.assertIn('require_version("Gtk", "3.0")', source, relative)


if __name__ == "__main__":
    unittest.main()
