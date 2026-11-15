"""Headless tests for the desktop shortcut model (``ui/shortcuts.py``)."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHORTCUTS_PATH = ROOT / "apps/socket_stream_desktop/ui/shortcuts.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHORTCUTS = load("shortcuts", SHORTCUTS_PATH)
Accelerator = SHORTCUTS.Accelerator
ShortcutMap = SHORTCUTS.ShortcutMap
ShortcutError = SHORTCUTS.ShortcutError


class AcceleratorParseTests(unittest.TestCase):
    def test_parse_and_display_round_trip(self):
        for value in SHORTCUTS.DEFAULT_BINDINGS.values():
            accel = Accelerator.parse(value)
            self.assertEqual(Accelerator.parse(accel.display()), accel, value)

    def test_display_is_canonical(self):
        self.assertEqual(Accelerator.parse("ctrl+alt+shift+o").display(), "Ctrl+Alt+Shift+O")
        self.assertEqual(Accelerator.parse("CTRL+ALT+S").display(), "Ctrl+Alt+S")
        self.assertEqual(Accelerator.parse("ctrl+alt+comma").display(), "Ctrl+Alt+Comma")
        self.assertEqual(Accelerator.parse("ctrl+f1").display(), "Ctrl+F1")
        self.assertEqual(Accelerator.parse("meta+shift+1").display(), "Shift+Meta+1")

    def test_modifier_aliases(self):
        for alias in ("ctrl+s", "control+s"):
            self.assertTrue(Accelerator.parse(alias).ctrl)
        for alias in ("alt+s", "opt+s", "option+s"):
            self.assertTrue(Accelerator.parse(alias).alt)
        for alias in ("meta+s", "cmd+s", "super+s", "win+s"):
            self.assertTrue(Accelerator.parse(alias).meta)

    def test_every_documented_key_parses(self):
        keys = ["a", "z", "0", "9", "f1", "f12", "f24", "space", "comma", "period",
                "slash", "semicolon", "apostrophe", "minus", "equals", "tab",
                "enter", "escape", "backspace"]
        for key in keys:
            self.assertEqual(Accelerator.parse(f"ctrl+{key}").key, key)

    def test_gtk_syntax(self):
        self.assertEqual(Accelerator.parse("ctrl+alt+shift+o").as_gtk(), "<Control><Alt><Shift>O")
        self.assertEqual(Accelerator.parse("ctrl+alt+comma").as_gtk(), "<Control><Alt>Comma")

    def test_rejections(self):
        bad = ["", "   ", "ctrl++s", "ctrl+", "ctrl", "ctrl+shift", "ctrl+s+x",
               "ctrl+ctrl+s", "hyper+s", "ctrl+f25", "ctrl+f0", "ctrl+bogus", "ctrl+!"]
        for value in bad:
            with self.assertRaises(ShortcutError, msg=value):
                Accelerator.parse(value)

    def test_rejection_messages_are_clear(self):
        with self.assertRaises(ShortcutError) as ctx:
            Accelerator.parse("hyper+s")
        self.assertIn("hyper", str(ctx.exception))
        with self.assertRaises(ShortcutError) as ctx:
            Accelerator.parse("ctrl")
        self.assertIn("non-modifier", str(ctx.exception))


class ShortcutMapTests(unittest.TestCase):
    def test_defaults_cover_every_action(self):
        self.assertEqual(len(SHORTCUTS.DEFAULT_BINDINGS), 12)
        self.assertEqual(len(SHORTCUTS.ACTIONS), 12)
        shortcut_map = ShortcutMap.defaults()
        self.assertEqual(len(shortcut_map.actions()), 12)
        for action in SHORTCUTS.ACTIONS:
            self.assertIsNotNone(shortcut_map.accelerator_for(action))
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Alt+S")

    def test_action_for(self):
        shortcut_map = ShortcutMap.defaults()
        self.assertEqual(shortcut_map.action_for("ctrl+alt+s"), "start")
        self.assertIsNone(shortcut_map.action_for("ctrl+alt+z"))
        self.assertIsNone(shortcut_map.action_for("not-an-accelerator"))

    def test_dispatch_to_registered_callback(self):
        shortcut_map = ShortcutMap.defaults()
        calls = []
        shortcut_map.register("start", lambda: calls.append("start"))
        self.assertTrue(shortcut_map.dispatch("ctrl+alt+s"))
        self.assertEqual(calls, ["start"])

    def test_dispatch_returns_false_for_unknown_action(self):
        shortcut_map = ShortcutMap.defaults()
        self.assertFalse(shortcut_map.dispatch("ctrl+alt+z"))
        self.assertFalse(shortcut_map.dispatch("garbage"))
        # A known action with no registered callback is also not dispatched.
        self.assertFalse(shortcut_map.dispatch("ctrl+alt+q"))

    def test_register_rejects_unknown_action(self):
        shortcut_map = ShortcutMap.defaults()
        with self.assertRaises(ShortcutError):
            shortcut_map.register("not_an_action", lambda: None)

    def test_parse_bindings_overrides_defaults(self):
        shortcut_map = SHORTCUTS.parse_bindings({"start": "ctrl+shift+1"})
        self.assertEqual(len(shortcut_map.actions()), 12)
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Shift+1")
        self.assertEqual(shortcut_map.accelerator_for("stop").display(), "Ctrl+Alt+X")

    def test_parse_bindings_rejects_invalid_entries(self):
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"start": "ctrl+bogus"})
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"not_an_action": "ctrl+alt+s"})
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"start": "ctrl+alt+x"})  # collides with stop

    def test_describe_lists_every_action(self):
        text = ShortcutMap.defaults().describe()
        for action in SHORTCUTS.ACTIONS:
            self.assertIn(action, text)


class ConfigFileTests(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            shortcut_map = SHORTCUTS.from_config_file(Path(tmp) / "nope.json")
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Alt+S")

    def test_malformed_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            shortcut_map = SHORTCUTS.from_config_file(path)
        self.assertEqual(len(shortcut_map.actions()), 12)
        self.assertEqual(shortcut_map.accelerator_for("quit").display(), "Ctrl+Alt+Q")

    def test_invalid_binding_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"shortcuts": {"bindings": {"start": "ctrl+bogus"}}}),
                            encoding="utf-8")
            shortcut_map = SHORTCUTS.from_config_file(path)
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Alt+S")

    def test_valid_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "shortcuts": {"enabled": True, "bindings": {"start": "ctrl+shift+1"}},
            }), encoding="utf-8")
            shortcut_map = SHORTCUTS.from_config_file(path)
            self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Shift+1")
            self.assertTrue(SHORTCUTS.enabled_from_config_file(path))

    def test_enabled_flag_defaults_to_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"shortcuts": {"enabled": False}}), encoding="utf-8")
            self.assertFalse(SHORTCUTS.enabled_from_config_file(path))
        self.assertTrue(SHORTCUTS.enabled_from_config_file(Path("/nonexistent/config.json")))

    def test_default_config_path_honours_env(self):
        import os
        saved = os.environ.get("SOCKET_STREAM_CONFIG")
        try:
            os.environ["SOCKET_STREAM_CONFIG"] = "/tmp/ctxlens.json"
            self.assertEqual(str(SHORTCUTS.default_config_path()), "/tmp/ctxlens.json")
        finally:
            if saved is None:
                os.environ.pop("SOCKET_STREAM_CONFIG", None)
            else:
                os.environ["SOCKET_STREAM_CONFIG"] = saved


class PlatformNoteTests(unittest.TestCase):
    def test_platform_note_is_honest_per_platform(self):
        self.assertIn("sxhkd", SHORTCUTS.platform_note("x11"))
        self.assertIn("GlobalShortcuts", SHORTCUTS.platform_note("wayland"))
        self.assertTrue(SHORTCUTS.platform_note("unknown").strip())
        self.assertTrue(SHORTCUTS.platform_note().strip())

    def test_detect_platform_reads_env(self):
        self.assertIn(SHORTCUTS.detect_platform(), ("wayland", "x11", "unknown"))


class ImportIsolationTests(unittest.TestCase):
    def test_importing_shortcuts_does_not_import_gi(self):
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('shortcuts_mod', r'{SHORTCUTS_PATH}')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print('gi' in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
