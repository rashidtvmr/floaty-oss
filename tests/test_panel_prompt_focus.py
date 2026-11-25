"""Headless tests for the floating panel's explicit prompt focus policy.

The panel is a nonactivating on-top overlay, so keyboard focus is relaxed
only for an explicit interaction with the prompt entry and restored when
focus leaves the entry or the whole window. These tests exercise the pure
focus/dispatch decisions with lightweight stubs and never import GTK.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import floating_overlay as overlay  # noqa: E402

SOURCE_PATH = UI / "floating_overlay.py"


class FakeGLib:
    """Minimal GLib stand-in that records idle sources and removals."""

    def __init__(self):
        self.sources = {}
        self.removed = []
        self._next = 1

    def idle_add(self, callback, *args):
        source = self._next
        self._next += 1
        self.sources[source] = (callback, args)
        return source

    def source_remove(self, source):
        self.removed.append(source)
        self.sources.pop(source, None)


class FakeWindow:
    def __init__(self, active=True):
        self.accept_focus = None
        self.focus_on_map = None
        self.presented = 0
        self.present_with_time_calls = []
        self.active = active
        self.hidden = False

    def set_accept_focus(self, value):
        self.accept_focus = value

    def set_focus_on_map(self, value):
        self.focus_on_map = value

    def present(self):
        self.presented += 1

    def present_with_time(self, timestamp):
        self.present_with_time_calls.append(timestamp)

    def is_active(self):
        return self.active

    def get_property(self, name):
        return self.active if name == "is-active" else None

    def hide(self):
        self.hidden = True


class FakeEntry:
    def __init__(self, text=""):
        self.text = text
        self.focused = False

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.text = value

    def grab_focus(self):
        self.focused = True


def make_panel(*, text="", active=True, on_ask=None):
    panel = overlay.FloatingOverlay.__new__(overlay.FloatingOverlay)
    panel.window = FakeWindow(active=active)
    panel.prompt_entry = FakeEntry(text)
    panel._GLib = FakeGLib()
    panel._on_ask = on_ask
    panel._nonactivating = True
    panel._entry_focused = False
    panel._focus_restore_source = None
    return panel


def event_with_time(timestamp):
    return type("Event", (), {"time": timestamp})()


class NonactivatingDefaults(unittest.TestCase):
    def test_default_is_nonactivating_with_focus_on_map_off(self):
        panel = make_panel()
        panel._set_nonactivating(True)
        self.assertFalse(panel.window.accept_focus)
        self.assertFalse(panel.window.focus_on_map)
        self.assertTrue(panel._nonactivating)

    def test_relaxing_focus_still_pins_focus_on_map_off(self):
        panel = make_panel()
        panel._set_nonactivating(False)
        self.assertTrue(panel.window.accept_focus)
        # focus_on_map must never be re-enabled, or a later map steals focus.
        self.assertFalse(panel.window.focus_on_map)
        self.assertFalse(panel._nonactivating)


class PermitPromptFocus(unittest.TestCase):
    def test_click_permits_focus_with_the_real_event_timestamp(self):
        panel = make_panel()
        result = panel.permit_prompt_focus(event_with_time(123456))
        self.assertFalse(result)  # GTK must still place the text cursor
        self.assertTrue(panel.window.accept_focus)
        self.assertEqual(panel.window.present_with_time_calls, [123456])
        self.assertEqual(panel.window.presented, 0)
        self.assertTrue(panel.prompt_entry.focused)

    def test_zero_timestamp_falls_back_to_plain_present(self):
        panel = make_panel()
        panel.permit_prompt_focus(event_with_time(0))
        self.assertEqual(panel.window.present_with_time_calls, [])
        self.assertEqual(panel.window.presented, 1)

    def test_missing_event_uses_plain_present(self):
        panel = make_panel()
        panel.permit_prompt_focus(None)
        self.assertEqual(panel.window.present_with_time_calls, [])
        self.assertEqual(panel.window.presented, 1)

    def test_focus_request_is_best_effort_when_window_rejects_it(self):
        class HostileWindow(FakeWindow):
            def present(self):
                raise RuntimeError("no compositor")

            def present_with_time(self, timestamp):
                raise RuntimeError("no compositor")

        panel = make_panel()
        panel.window = HostileWindow()
        self.assertFalse(panel.permit_prompt_focus(event_with_time(7)))
        self.assertTrue(panel.window.accept_focus)

    def test_generic_focus_request_does_not_grab_a_specific_widget(self):
        panel = make_panel()
        result = panel.permit_focus(event_with_time(321))
        self.assertFalse(result)
        self.assertTrue(panel.window.accept_focus)
        self.assertEqual(panel.window.present_with_time_calls, [321])
        # No explicit widget: GTK's own click handler focuses the target.
        self.assertFalse(panel.prompt_entry.focused)


class SelectableFocus(unittest.TestCase):
    def test_clicking_selectable_text_permits_focus(self):
        panel = make_panel()
        result = panel._on_selectable_press(None, event_with_time(88))
        self.assertFalse(result)  # GTK must still run its selection handler
        self.assertTrue(panel.window.accept_focus)
        self.assertEqual(panel.window.present_with_time_calls, [88])
        self.assertFalse(panel.prompt_entry.focused)


class RestoreOnLeavingEntry(unittest.TestCase):
    def test_focus_out_schedules_an_idle_restore(self):
        panel = make_panel()
        panel._on_prompt_focus_out()
        self.assertFalse(panel._entry_focused)
        self.assertIsNotNone(panel._focus_restore_source)
        callback, _args = panel._GLib.sources[panel._focus_restore_source]
        self.assertEqual(callback, panel._focus_restore_if_left)

    def test_restore_waits_while_the_window_is_still_active(self):
        panel = make_panel(active=True)
        panel.permit_prompt_focus(event_with_time(5))
        panel._on_prompt_focus_out()
        panel._focus_restore_if_left()
        # Focus moved to another panel widget (or none), but the toplevel
        # still owns keyboard focus, so selection must keep working.
        self.assertTrue(panel.window.accept_focus)
        self.assertIsNone(panel._focus_restore_source)

    def test_restore_runs_once_the_window_is_inactive(self):
        panel = make_panel(active=False)
        panel.permit_prompt_focus(event_with_time(5))
        panel._focus_restore_if_left()
        self.assertFalse(panel.window.accept_focus)

    def test_window_active_change_restores_only_when_inactive(self):
        panel = make_panel(active=True)
        panel.permit_prompt_focus(event_with_time(5))
        panel._on_window_active_changed(panel.window, None)
        self.assertTrue(panel.window.accept_focus)
        panel.window.active = False
        panel._on_window_active_changed(panel.window, None)
        self.assertFalse(panel.window.accept_focus)

    def test_restore_cancels_a_pending_idle_source(self):
        panel = make_panel()
        panel._focus_restore_source = 42
        panel._GLib.sources[42] = (panel._focus_restore_if_left, ())
        panel.restore_nonactivating()
        self.assertIn(42, panel._GLib.removed)
        self.assertIsNone(panel._focus_restore_source)
        self.assertFalse(panel.window.accept_focus)

    def test_focus_in_cancels_a_pending_restore(self):
        panel = make_panel()
        panel._on_prompt_focus_out()
        pending = panel._focus_restore_source
        panel._on_prompt_focus_in()
        self.assertIn(pending, panel._GLib.removed)
        self.assertIsNone(panel._focus_restore_source)
        self.assertTrue(panel._entry_focused)


class PromptDispatch(unittest.TestCase):
    def test_empty_or_whitespace_enter_does_not_dispatch(self):
        calls = []
        panel = make_panel(text="   \n  ", on_ask=calls.append)
        panel._on_prompt_activate()
        self.assertEqual(calls, [])

    def test_missing_callback_is_safe(self):
        panel = make_panel(text="a real question", on_ask=None)
        panel._on_prompt_activate()  # must not raise
        self.assertEqual(panel.prompt_entry.get_text(), "a real question")

    def test_valid_enter_dispatches_once_and_clears_before_callback(self):
        seen = []

        def ask(text):
            seen.append((text, panel.prompt_entry.get_text()))

        panel = make_panel(text="  What is 2 + 2?  ", on_ask=ask)
        panel._on_prompt_activate()
        # A second activation for the same gesture sees an empty entry.
        panel._on_prompt_activate()
        self.assertEqual(seen, [("What is 2 + 2?", "")])
        self.assertEqual(panel.prompt_entry.get_text(), "")


class TeardownCleanup(unittest.TestCase):
    def test_hide_restores_nonactivating_and_clears_focus(self):
        panel = make_panel()
        panel.permit_prompt_focus(event_with_time(9))
        panel._entry_focused = True
        panel.hide()
        self.assertTrue(panel.window.hidden)
        self.assertFalse(panel.window.accept_focus)
        self.assertFalse(panel._entry_focused)


class SourceContract(unittest.TestCase):
    """The wiring must exist in the real module, not only in stubs."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        cls.klass = next(node for node in ast.walk(cls.tree)
                         if isinstance(node, ast.ClassDef) and node.name == "FloatingOverlay")

    def method(self, name):
        return next(node for node in self.klass.body
                    if isinstance(node, ast.FunctionDef) and node.name == name)

    def connect_pairs(self, function):
        pairs = []
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                pairs.append((ast.unparse(node.func.value), node.args[0].value))
        return pairs

    def test_constructor_wires_prompt_and_window_focus_signals(self):
        pairs = self.connect_pairs(self.method("__init__"))
        for expected in (
            ("self.prompt_entry", "button-press-event"),
            ("self.prompt_entry", "focus-in-event"),
            ("self.prompt_entry", "focus-out-event"),
            ("self.window", "notify::is-active"),
            ("self.answer", "button-press-event"),
        ):
            self.assertIn(expected, pairs)
        self.assertNotIn(("self.full_question", "button-press-event"), pairs)
        self.assertIn("on_press=self._on_selectable_press", ast.unparse(self.method("__init__")))

    def test_constructor_keeps_the_panel_nonactivating_by_default(self):
        calls = [(ast.unparse(node.func), [ast.unparse(arg) for arg in node.args])
                 for node in ast.walk(self.method("__init__"))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertIn(("self.window.set_accept_focus", ["False"]), calls)
        self.assertIn(("self.window.set_focus_on_map", ["False"]), calls)

    def test_focus_policy_never_touches_the_window_type_hint(self):
        # Focus is relaxed through the WM input hint only; no compositor or
        # exclusion behaviour may be changed to make typing work.
        for name in ("_set_nonactivating", "permit_focus", "permit_prompt_focus",
                     "restore_nonactivating", "_on_selectable_press"):
            attrs = {node.attr for node in ast.walk(self.method(name))
                     if isinstance(node, ast.Attribute)}
            self.assertNotIn("set_type_hint", attrs, name)
            self.assertNotIn("set_override_redirect", attrs, name)

    def test_cursor_blending_stays_separate_from_the_focus_policy(self):
        called = {node.func.attr for node in ast.walk(self.method("set_cursor_blending"))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(called & {"_set_nonactivating", "permit_focus",
                                   "permit_prompt_focus", "restore_nonactivating"})

    def test_show_never_auto_focuses_the_prompt(self):
        function = self.method("show")
        attrs = {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)}
        called = {node.func.attr for node in ast.walk(function)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("grab_focus", attrs)
        self.assertNotIn("permit_prompt_focus", called)

    def test_destroy_cancels_a_pending_focus_restore(self):
        called = {node.func.attr for node in ast.walk(self.method("destroy"))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertIn("_cancel_focus_restore", called)

    def test_main_persists_the_standalone_cursor_blending_choice(self):
        main_fn = next(node for node in ast.walk(self.tree)
                       if isinstance(node, ast.FunctionDef) and node.name == "main")
        names = {node.id for node in ast.walk(main_fn) if isinstance(node, ast.Name)}
        self.assertIn("persist_cursor_blending", names)
        keywords = {kw.arg for node in ast.walk(main_fn)
                    if isinstance(node, ast.Call) for kw in node.keywords}
        self.assertIn("on_cursor_blending", keywords)


class StandaloneCursorBlendingPersistence(unittest.TestCase):
    """The standalone panel must save the toolbar toggle itself."""

    def test_helper_is_called_with_the_new_boolean(self):
        calls = []
        ok, message = overlay.persist_cursor_blending(False, save=calls.append)
        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(calls, [False])

    def test_save_failure_is_reported_not_raised(self):
        def boom(_enabled):
            raise OSError("read-only config")

        ok, message = overlay.persist_cursor_blending(True, save=boom)
        self.assertFalse(ok)
        self.assertIn("cursor blending", message.lower())

    def test_shortcut_error_is_reported_not_raised(self):
        import shortcuts

        def boom(_enabled):
            raise shortcuts.ShortcutError("invalid cursor_blending")

        ok, message = overlay.persist_cursor_blending(False, save=boom)
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_default_path_uses_the_single_key_helper(self):
        from unittest import mock

        import shortcuts

        with mock.patch.object(shortcuts, "save_cursor_blending", create=True) as helper:
            ok, message = overlay.persist_cursor_blending(True)
        self.assertTrue(ok, message)
        helper.assert_called_once_with(True)

    def test_real_helper_preserves_other_appearance(self):
        import os
        import tempfile

        import shortcuts

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            shortcuts.save_appearance(
                {"opacity": 0.75, "font_size": 17, "reduced_motion": False,
                 "cursor_blending": True},
                path=path,
            )
            previous = os.environ.get("SOCKET_STREAM_CONFIG")
            os.environ["SOCKET_STREAM_CONFIG"] = str(path)
            try:
                ok, message = overlay.persist_cursor_blending(False)
            finally:
                if previous is None:
                    os.environ.pop("SOCKET_STREAM_CONFIG", None)
                else:
                    os.environ["SOCKET_STREAM_CONFIG"] = previous
            self.assertTrue(ok, message)
            reloaded = shortcuts.load_appearance(path)
            self.assertFalse(reloaded["cursor_blending"])
            self.assertEqual(reloaded["opacity"], 0.75)
            self.assertEqual(reloaded["font_size"], 17)
            self.assertFalse(reloaded["reduced_motion"])


if __name__ == "__main__":
    unittest.main()
