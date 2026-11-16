"""Headless tests for the desktop shortcut model (``ui/shortcuts.py``)."""
import errno
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(Accelerator.parse("ctrl+alt+left").display(), "Ctrl+Alt+Left")

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
                "enter", "escape", "backspace", "left", "right", "up", "down"]
        for key in keys:
            self.assertEqual(Accelerator.parse(f"ctrl+{key}").key, key)

    def test_gtk_syntax(self):
        self.assertEqual(Accelerator.parse("ctrl+alt+shift+o").as_gtk(), "<Control><Alt><Shift>O")
        self.assertEqual(Accelerator.parse("ctrl+alt+comma").as_gtk(), "<Control><Alt>Comma")
        self.assertEqual(Accelerator.parse("ctrl+alt+left").as_gtk(), "<Control><Alt>Left")

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
        self.assertEqual(len(SHORTCUTS.DEFAULT_BINDINGS), 17)
        self.assertEqual(len(SHORTCUTS.ACTIONS), 17)
        shortcut_map = ShortcutMap.defaults()
        self.assertEqual(len(shortcut_map.actions()), 17)
        for action in SHORTCUTS.ACTIONS:
            self.assertIsNotNone(shortcut_map.accelerator_for(action))
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Alt+S")
        self.assertEqual(shortcut_map.accelerator_for("listen_audio").display(), "Ctrl+Alt+A")
        self.assertEqual(shortcut_map.accelerator_for("pin_question").display(), "Ctrl+Alt+B")
        self.assertEqual(shortcut_map.accelerator_for("previous_question").display(), "Ctrl+Alt+Left")
        self.assertEqual(shortcut_map.accelerator_for("next_question").display(), "Ctrl+Alt+Right")
        self.assertEqual(shortcut_map.accelerator_for("follow_latest").display(), "Ctrl+Alt+L")

    def test_action_labels_present(self):
        self.assertIn("listen_audio", SHORTCUTS.ACTION_LABELS)
        self.assertIn("pin_question", SHORTCUTS.ACTION_LABELS)
        self.assertIn("previous_question", SHORTCUTS.ACTION_LABELS)
        self.assertEqual(SHORTCUTS.ACTION_LABELS["listen_audio"], "Listen audio")

    def test_action_for(self):
        shortcut_map = ShortcutMap.defaults()
        self.assertEqual(shortcut_map.action_for("ctrl+alt+s"), "start")
        self.assertEqual(shortcut_map.action_for("ctrl+alt+left"), "previous_question")
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
        self.assertFalse(shortcut_map.dispatch("ctrl+alt+q"))

    def test_register_rejects_unknown_action(self):
        shortcut_map = ShortcutMap.defaults()
        with self.assertRaises(ShortcutError):
            shortcut_map.register("not_an_action", lambda: None)

    def test_parse_bindings_overrides_defaults(self):
        shortcut_map = SHORTCUTS.parse_bindings({"start": "ctrl+shift+1"})
        self.assertEqual(len(shortcut_map.actions()), 17)
        self.assertEqual(shortcut_map.accelerator_for("start").display(), "Ctrl+Shift+1")
        self.assertEqual(shortcut_map.accelerator_for("stop").display(), "Ctrl+Alt+X")

    def test_parse_bindings_rejects_invalid_entries(self):
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"start": "ctrl+bogus"})
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"not_an_action": "ctrl+alt+s"})
        with self.assertRaises(ShortcutError):
            SHORTCUTS.parse_bindings({"start": "ctrl+alt+x"})

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
        self.assertEqual(len(shortcut_map.actions()), 17)
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
        saved = os.environ.get("SOCKET_STREAM_CONFIG")
        try:
            os.environ["SOCKET_STREAM_CONFIG"] = "/tmp/ctxlens.json"
            self.assertEqual(str(SHORTCUTS.default_config_path()), "/tmp/ctxlens.json")
        finally:
            if saved is None:
                os.environ.pop("SOCKET_STREAM_CONFIG", None)
            else:
                os.environ["SOCKET_STREAM_CONFIG"] = saved

    def test_missing_home_uses_the_same_relative_fallback_as_rust(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(SHORTCUTS.default_config_path(), Path(".socket_stream.json"))


class SaveConfigFileTests(unittest.TestCase):
    def test_save_then_roundtrip_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            # Seed with unrelated fields and unknown nested shortcut keys
            path.write_text(json.dumps({
                "a11y": {"mode": "tree"},
                "ocr": {"enabled": True},
                "shortcuts": {"enabled": True, "bindings": {"start": "ctrl+alt+s"}, "custom_nested": {"keep": 1}},
                "extra_top": "hello",
            }), encoding="utf-8")
            out = SHORTCUTS.save_config_file({"start": "ctrl+shift+1", "listen_audio": "ctrl+alt+a"}, enabled=True, path=path)
            self.assertEqual(out, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["a11y"]["mode"], "tree")
            self.assertEqual(data["ocr"]["enabled"], True)
            self.assertEqual(data["extra_top"], "hello")
            self.assertEqual(data["shortcuts"]["custom_nested"], {"keep": 1})
            self.assertEqual(data["shortcuts"]["bindings"]["start"], "ctrl+shift+1")
            self.assertEqual(data["shortcuts"]["bindings"]["listen_audio"], "ctrl+alt+a")
            # roundtrip via from_config_file
            sm = SHORTCUTS.from_config_file(path)
            self.assertEqual(sm.accelerator_for("start").display(), "Ctrl+Shift+1")
            self.assertEqual(sm.accelerator_for("listen_audio").display(), "Ctrl+Alt+A")

    def test_save_validates_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"keep": 1}), encoding="utf-8")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+bogus"}, path=path)
            # file unchanged
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"keep": 1})

    def test_save_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+x", "stop": "ctrl+alt+x"}, path=path)

    def test_save_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.json"
            real.write_text("{}", encoding="utf-8")
            link = Path(tmp) / "link.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=link)

    def test_save_refuses_malformed_existing_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=path)
            # unchanged
            self.assertTrue(path.read_text(encoding="utf-8").startswith("{not"))

    def test_save_refuses_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link.json"
            try:
                link.symlink_to(Path(tmp) / "does_not_exist.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=link)
            # symlink still there (not replaced into a real file by clobber in bad way)
            self.assertTrue(link.is_symlink())

    def test_save_sets_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=path)
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


class AppearanceTests(unittest.TestCase):
    def test_load_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            app = SHORTCUTS.load_appearance(path)
            self.assertEqual(app["opacity"], 0.90)
            self.assertEqual(app["font_size"], 16)
            self.assertEqual(app["reduced_motion"], False)

    def test_save_then_load_appearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            SHORTCUTS.save_appearance({"opacity": 0.8, "font_size": 18, "reduced_motion": True}, path=path)
            app = SHORTCUTS.load_appearance(path)
            self.assertAlmostEqual(app["opacity"], 0.8)
            self.assertEqual(app["font_size"], 18)
            self.assertEqual(app["reduced_motion"], True)

    def test_save_appearance_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"shortcuts": {"enabled": False}, "other": 1}), encoding="utf-8")
            SHORTCUTS.save_appearance({"opacity": 0.9, "font_size": 16, "reduced_motion": False}, path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["other"], 1)
            self.assertEqual(data["shortcuts"]["enabled"], False)

    def test_save_appearance_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_appearance({"opacity": 0.9, "font_size": 16, "reduced_motion": False, "bad": 1}, path=path)

    def test_load_appearance_refuses_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link.json"
            try:
                link.symlink_to(Path(tmp) / "nope.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.load_appearance(path=link)

    def test_save_appearance_refuses_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link.json"
            try:
                link.symlink_to(Path(tmp) / "nope.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_appearance({"opacity": 0.9, "font_size": 16, "reduced_motion": False}, path=link)

    def test_save_appearance_rejects_bad_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_appearance({"opacity": 0.1, "font_size": 16, "reduced_motion": False}, path=path)
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_appearance({"opacity": 0.9, "font_size": 30, "reduced_motion": False}, path=path)
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_appearance({"opacity": 0.9, "font_size": 16, "reduced_motion": "no"}, path=path)


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


SAVE_SHORTCUTS_SCRIPT = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("shortcuts_mod", os.environ["SHORTCUTS_PATH"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.save_config_file({"start": "ctrl+shift+1"}, path=Path(sys.argv[1]))
"""

SAVE_APPEARANCE_SCRIPT = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("shortcuts_mod", os.environ["SHORTCUTS_PATH"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.save_appearance({"opacity": 0.61, "font_size": 22, "reduced_motion": True},
                       path=Path(sys.argv[1]))
"""

HOLD_LOCK_SCRIPT = """
import fcntl, json, os, sys, time
from pathlib import Path
config = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
payload = json.loads(sys.argv[4])
lock = config.with_name(config.name + ".lock")
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
ready.write_text("ready", encoding="utf-8")
while not release.exists():
    time.sleep(0.01)
tmp = config.with_name(config.name + ".holder.tmp")
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, config)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""


def _child_env():
    env = os.environ.copy()
    env["SHORTCUTS_PATH"] = str(SHORTCUTS_PATH)
    return env


def _wait_for_file(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def _no_temp_files(directory, name="config.json"):
    return [entry.name for entry in Path(directory).iterdir()
            if entry.name.startswith(f".{name}.")]


class StrictConfigWriteTests(unittest.TestCase):
    """Malformed input, non-regular paths and symlinks must never be clobbered."""

    MALFORMED_ROOT = ("", "   \n", "{not json", "[]", "1", "\"text\"", "null")
    MALFORMED_SHORTCUTS = ("{\"shortcuts\": []}", "{\"shortcuts\": \"text\"}",
                           "{\"shortcuts\": null}")
    MALFORMED_DESKTOP = ("{\"desktop\": 5}", "{\"desktop\": null}",
                         "{\"desktop\": {\"appearance\": null}}", "{\"desktop\": {\"appearance\": 5}}",
                         "{\"desktop\": {\"appearance\": {\"opacity\": 2}}}")

    def test_malformed_root_or_shortcuts_section_is_refused(self):
        for raw in self.MALFORMED_ROOT + self.MALFORMED_SHORTCUTS:
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.json"
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises((ShortcutError, ValueError)):
                        SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=path)
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)
                    self.assertEqual(_no_temp_files(tmp), [])

    def test_malformed_unrelated_section_is_preserved_not_clobbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"desktop": 5}', encoding="utf-8")
            SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["desktop"], 5)
            self.assertEqual(len(data["shortcuts"]["bindings"]), 17)

    def test_malformed_config_makes_appearance_loader_raise_not_default(self):
        for raw in self.MALFORMED_ROOT + self.MALFORMED_DESKTOP:
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.json"
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises((ShortcutError, ValueError)):
                        SHORTCUTS.load_appearance(path)

    def test_appearance_loader_ignores_unrelated_shortcuts_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"shortcuts": []}', encoding="utf-8")
            self.assertEqual(SHORTCUTS.load_appearance(path),
                             dict(SHORTCUTS.DEFAULT_APPEARANCE))

    def test_best_effort_loaders_fall_back_on_malformed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(SHORTCUTS.from_config_file(path).accelerator_for("start").display(),
                             "Ctrl+Alt+S")
            self.assertTrue(SHORTCUTS.enabled_from_config_file(path))

    def test_save_refuses_symlinked_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.json"
            victim.write_text('{"keep": 1}', encoding="utf-8")
            lock = Path(tmp) / "config.json.lock"
            try:
                lock.symlink_to(victim)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=Path(tmp) / "config.json")
            self.assertEqual(json.loads(victim.read_text(encoding="utf-8")), {"keep": 1})
            self.assertTrue(lock.is_symlink())

    def test_save_refuses_dangling_symlinked_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "config.json.lock"
            missing = Path(tmp) / "missing-lock-target"
            try:
                lock.symlink_to(missing)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=Path(tmp) / "config.json")
            self.assertTrue(lock.is_symlink())
            self.assertFalse(missing.exists())

    def test_save_refuses_directory_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.mkdir()
            with self.assertRaises(ShortcutError):
                SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=target)
            self.assertTrue(target.is_dir())

    def test_symlinked_config_is_refused_for_loaders(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.json"
            real.write_text(json.dumps({"shortcuts": {"enabled": False}}), encoding="utf-8")
            link = Path(tmp) / "config.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            # Strict loader raises; fallback loaders use defaults, never the link.
            with self.assertRaises(ShortcutError):
                SHORTCUTS.load_appearance(link)
            self.assertTrue(SHORTCUTS.from_config_file(link)
                            .accelerator_for("start").display() == "Ctrl+Alt+S")
            self.assertTrue(SHORTCUTS.enabled_from_config_file(link))

    def test_fifo_config_path_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "config.json"
            os.mkfifo(fifo)
            result = subprocess.run(
                [sys.executable, "-c", SAVE_SHORTCUTS_SCRIPT, str(fifo)],
                capture_output=True, text=True, timeout=10, env=_child_env(), check=False)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("non-regular", result.stderr)
            self.assertTrue(fifo.exists())

    def test_fifo_lock_path_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            os.mkfifo(Path(tmp) / "config.json.lock")
            result = subprocess.run(
                [sys.executable, "-c", SAVE_SHORTCUTS_SCRIPT, str(config)],
                capture_output=True, text=True, timeout=10, env=_child_env(), check=False)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("non-regular", result.stderr)
            self.assertFalse(config.exists())

    def test_fifo_config_read_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "config.json"
            os.mkfifo(fifo)
            code = (
                "import importlib.util, os, sys\n"
                "spec = importlib.util.spec_from_file_location('m', os.environ['SHORTCUTS_PATH'])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module.load_appearance(sys.argv[1])\n"
            )
            result = subprocess.run([sys.executable, "-c", code, str(fifo)],
                                    capture_output=True, text=True, timeout=10,
                                    env=_child_env(), check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-regular", result.stderr)
            fallback = subprocess.run(
                [sys.executable, "-c",
                 "import importlib.util, os, sys\n"
                 "spec = importlib.util.spec_from_file_location('m', os.environ['SHORTCUTS_PATH'])\n"
                 "module = importlib.util.module_from_spec(spec)\n"
                 "spec.loader.exec_module(module)\n"
                 "print(module.from_config_file(sys.argv[1]).accelerator_for('start').display())\n",
                 str(fifo)],
                capture_output=True, text=True, timeout=10, env=_child_env(), check=False)
            self.assertEqual(fallback.stdout.strip(), "Ctrl+Alt+S", fallback.stderr)

    def test_atomic_replace_failure_cleans_temp_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            original = '{"keep": 1}'
            config.write_text(original, encoding="utf-8")
            with mock.patch.object(SHORTCUTS.os, "replace",
                                   side_effect=OSError(errno.EIO, "replace failed")):
                with self.assertRaises(OSError):
                    SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=config)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(_no_temp_files(tmp), [])

    def test_serialization_failure_propagates_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            with self.assertRaises(ShortcutError):
                SHORTCUTS._lock_and_update(config, lambda existing: {"bad": {1, 2}})
            self.assertFalse(config.exists())
            self.assertEqual(_no_temp_files(tmp), [])

    def test_lock_descriptor_is_closed_when_mutator_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"

            def boom(_existing):
                raise RuntimeError("mutator failed")

            with self.assertRaises(RuntimeError):
                SHORTCUTS._lock_and_update(config, boom)
            lock = Path(tmp) / "config.json.lock"
            code = (
                "import fcntl, os, sys\n"
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "print('acquired')\n"
            )
            result = subprocess.run([sys.executable, "-c", code, str(lock)],
                                    capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.stdout.strip(), "acquired", result.stderr)
            self.assertFalse(config.exists())

    def test_save_config_file_is_documented_full_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "shortcuts": {"enabled": False,
                              "bindings": {"quit": "ctrl+shift+q"},
                              "unknown_sibling": {"keep": 1}},
                "desktop": {"appearance": {"opacity": 0.7, "font_size": 20,
                                            "reduced_motion": True}},
            }), encoding="utf-8")
            SHORTCUTS.save_config_file({"start": "ctrl+shift+1"}, path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            bindings = data["shortcuts"]["bindings"]
            self.assertEqual(len(bindings), 17)
            self.assertEqual(bindings["start"], "ctrl+shift+1")
            # Unspecified actions reset to defaults, and enabled defaults to true.
            self.assertEqual(bindings["quit"], "ctrl+alt+q")
            self.assertIs(data["shortcuts"]["enabled"], True)
            self.assertEqual(data["shortcuts"]["unknown_sibling"], {"keep": 1})
            self.assertEqual(data["desktop"]["appearance"]["font_size"], 20)

    def test_save_config_file_explicitly_disabled_is_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, enabled=False, path=path)
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))
                          ["shortcuts"]["enabled"], False)

    def test_lock_file_mode_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            SHORTCUTS.save_config_file({"start": "ctrl+alt+s"}, path=path)
            lock_mode = (Path(tmp) / "config.json.lock").stat().st_mode & 0o777
            self.assertEqual(lock_mode, 0o600)

    def test_save_appearance_refuses_nonobject_desktop_section(self):
        for raw in ('{"desktop": null}', '{"desktop": []}', '{"desktop": 5}'):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.json"
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises(ShortcutError):
                        SHORTCUTS.save_appearance(
                            {"opacity": 0.6, "font_size": 20, "reduced_motion": False},
                            path=path)
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_save_appearance_preserves_desktop_siblings_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "desktop": {"appearance": {"opacity": 0.9, "font_size": 16,
                                           "reduced_motion": False},
                            "window": {"width": 480}},
                "unknown_top": {"nested": [1, 2]},
                "shortcuts": {"enabled": False},
            }), encoding="utf-8")
            SHORTCUTS.save_appearance(
                {"opacity": 0.6, "font_size": 21, "reduced_motion": True}, path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["desktop"]["window"], {"width": 480})
            self.assertEqual(data["desktop"]["appearance"],
                             {"opacity": 0.6, "font_size": 21, "reduced_motion": True})
            self.assertEqual(data["unknown_top"], {"nested": [1, 2]})
            self.assertIs(data["shortcuts"]["enabled"], False)


class CooperativeLockingTests(unittest.TestCase):
    """Two cooperating writers must serialize on the shared ``.lock`` flock."""

    def _start_lock_holder(self, tmp, payload):
        config = Path(tmp) / "config.json"
        ready = Path(tmp) / "ready"
        release = Path(tmp) / "release"
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLD_LOCK_SCRIPT, str(config), str(ready),
             str(release), json.dumps(payload)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

        self.addCleanup(cleanup)
        self.assertTrue(_wait_for_file(ready, timeout=10),
                        proc.stderr.read() if proc.poll() is not None else "lock holder stalled")
        return config, release, proc

    def _start_writer(self, script, config):
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env())

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

        self.addCleanup(cleanup)
        return proc

    def test_shortcut_writer_waits_and_preserves_other_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, release, holder = self._start_lock_holder(
                tmp, {"a11y": {"mode": "text"}, "from_holder": True})
            writer = self._start_writer(SAVE_SHORTCUTS_SCRIPT, config)
            time.sleep(0.4)
            self.assertIsNone(writer.poll(), "shortcut save must wait for the held flock")
            release.write_text("go", encoding="utf-8")
            _, writer_err = writer.communicate(timeout=15)
            self.assertEqual(writer.returncode, 0, writer_err)
            _holder_out, holder_err = holder.communicate(timeout=15)
            self.assertEqual(holder.returncode, 0, holder_err)
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertIs(data["from_holder"], True)
            self.assertEqual(data["a11y"]["mode"], "text")
            self.assertEqual(data["shortcuts"]["bindings"]["start"], "ctrl+shift+1")
            self.assertEqual(len(data["shortcuts"]["bindings"]), 17)

    def test_appearance_writer_waits_and_preserves_shortcuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, release, holder = self._start_lock_holder(
                tmp, {"shortcuts": {"enabled": False,
                                    "bindings": {"start": "ctrl+shift+9"}},
                      "from_holder": "shortcuts"})
            writer = self._start_writer(SAVE_APPEARANCE_SCRIPT, config)
            time.sleep(0.4)
            self.assertIsNone(writer.poll(), "appearance save must wait for the held flock")
            release.write_text("go", encoding="utf-8")
            _, writer_err = writer.communicate(timeout=15)
            self.assertEqual(writer.returncode, 0, writer_err)
            _holder_out, holder_err = holder.communicate(timeout=15)
            self.assertEqual(holder.returncode, 0, holder_err)
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["from_holder"], "shortcuts")
            self.assertEqual(data["shortcuts"]["bindings"]["start"], "ctrl+shift+9")
            self.assertIs(data["shortcuts"]["enabled"], False)
            self.assertEqual(data["desktop"]["appearance"]["opacity"], 0.61)
            self.assertEqual(data["desktop"]["appearance"]["font_size"], 22)
            self.assertIs(data["desktop"]["appearance"]["reduced_motion"], True)


if __name__ == "__main__":
    unittest.main()
