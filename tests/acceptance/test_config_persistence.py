"""Cross-language config persistence acceptance tests.

These exercise the real Rust CLI (`socket_stream hotkeys`) against files the
Python desktop shell writes, and prove the shared `fcntl.flock` convention
serializes a Python writer against the Rust writer.

They skip cleanly when the CLI has not been built. Build it with:

    CARGO_TARGET_DIR=<dir> cargo build -p socket_stream

No arbitrary repeated builds: the suite only locates an existing binary and
honours `CARGO_TARGET_DIR` when it is set.
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHORTCUTS_PATH = ROOT / "apps/socket_stream_desktop/ui/shortcuts.py"


def _candidate_binaries():
    candidates = [
        ROOT / "target/debug/socket_stream",
        ROOT / "target/debug/bin/socket_stream",
        ROOT / "bin/socket_stream",
    ]
    cargo_target_dir = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target_dir:
        candidates.insert(0, Path(cargo_target_dir) / "debug/socket_stream")
    return candidates


def locate_cli():
    return next((path for path in _candidate_binaries() if path.is_file()), None)


def load_shortcuts():
    spec = importlib.util.spec_from_file_location("shortcuts_acceptance", SHORTCUTS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigPersistenceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = locate_cli()
        if cls.binary is None:
            raise unittest.SkipTest(
                "socket_stream is not built; run "
                "CARGO_TARGET_DIR=<dir> cargo build -p socket_stream")
        cls.shortcuts = load_shortcuts()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = self.tmp / "config.json"

    def env(self, path=None):
        env = os.environ.copy()
        env["SOCKET_STREAM_CONFIG"] = str(path or self.config)
        return env

    def run_cli(self, *args, path=None, timeout=30):
        return subprocess.run(
            [str(self.binary), *args],
            cwd=ROOT,
            env=self.env(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def seed(self):
        self.config.write_text(json.dumps({
            "a11y": {"mode": "focused", "unknown_a11y": [1]},
            "ocr": {"enabled": True},
            "overlay": {"compact": True},
            "desktop": {"appearance": {"opacity": 0.7, "font_size": 20,
                                       "reduced_motion": True},
                        "window": {"width": 480}},
            "provider": {"api_base": "http://127.0.0.1:11434/v1"},
            "unknown_top": {"deep": True},
            "shortcuts": {
                "enabled": False,
                "bindings": {"start": "ctrl+shift+1", "custom_action": "ctrl+alt+z"},
                "unknown_shortcuts": {"keep": 1},
            },
        }, indent=2), encoding="utf-8")

    def test_init_force_preserves_desktop_a11y_ocr_provider_and_unknown_siblings(self):
        self.seed()
        result = self.run_cli("hotkeys", "init", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reset shortcuts", result.stdout)

        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertIs(data["shortcuts"]["enabled"], True)
        bindings = data["shortcuts"]["bindings"]
        self.assertEqual(len(bindings), 17)
        self.assertEqual(bindings["start"], "ctrl+alt+s")
        self.assertEqual(bindings["previous_question"], "ctrl+alt+left")
        self.assertNotIn("custom_action", bindings)
        self.assertEqual(data["shortcuts"]["unknown_shortcuts"], {"keep": 1})
        self.assertEqual(data["desktop"]["appearance"]["opacity"], 0.7)
        self.assertEqual(data["desktop"]["window"], {"width": 480})
        self.assertEqual(data["a11y"], {"mode": "focused", "unknown_a11y": [1]})
        self.assertEqual(data["ocr"], {"enabled": True})
        self.assertEqual(data["overlay"], {"compact": True})
        self.assertEqual(data["provider"]["api_base"], "http://127.0.0.1:11434/v1")
        self.assertEqual(data["unknown_top"], {"deep": True})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

        # Round trip: the Python shell reads the Rust-written defaults.
        shortcut_map = self.shortcuts.from_config_file(self.config)
        self.assertEqual(len(shortcut_map.actions()), 17)
        self.assertTrue(self.shortcuts.enabled_from_config_file(self.config))
        self.assertEqual(shortcut_map.accelerator_for("next_question").display(),
                         "Ctrl+Alt+Right")

    def test_init_without_force_refuses_existing_file(self):
        self.seed()
        before = self.config.read_bytes()
        result = self.run_cli("hotkeys", "init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.config.read_bytes(), before)

    def test_home_missing_uses_the_shared_working_directory_fallback(self):
        env = os.environ.copy()
        for name in ("SOCKET_STREAM_CONFIG", "XDG_CONFIG_HOME", "HOME"):
            env.pop(name, None)
        result = subprocess.run([str(self.binary), "hotkeys", "init"], cwd=self.tmp,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.tmp / ".socket_stream.json"
        self.assertTrue(path.is_file())
        self.assertEqual(len(self.shortcuts.from_config_file(path).actions()), 17)

    def test_two_concurrent_init_runs_have_exactly_one_winner(self):
        procs = [
            subprocess.Popen(
                [str(self.binary), "hotkeys", "init"],
                cwd=ROOT,
                env=self.env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            results.append((proc.returncode, out, err))
        winners = [item for item in results if item[0] == 0]
        losers = [item for item in results if item[0] != 0]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIn("already exists", losers[0][2])
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(data["shortcuts"]["bindings"]), 17)
        self.assertIs(data["shortcuts"]["enabled"], True)

    def test_init_force_refuses_dangling_symlink(self):
        link = self.tmp / "config.json"
        missing = self.tmp / "missing.json"
        try:
            link.symlink_to(missing)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        for args in (("hotkeys", "init"), ("hotkeys", "init", "--force")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink", result.stderr)
                self.assertTrue(link.is_symlink())
                self.assertFalse(missing.exists())

    def test_init_force_refuses_malformed_config_without_clobbering(self):
        raw = "{not json"
        self.config.write_text(raw, encoding="utf-8")
        result = self.run_cli("hotkeys", "init", "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

    def test_list_reads_python_written_bindings(self):
        self.shortcuts.save_config_file(
            {"start": "ctrl+shift+1", "next_question": "ctrl+shift+2"},
            enabled=True,
            path=self.config,
        )
        result = self.run_cli("hotkeys", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ctrl+Shift+1", result.stdout)
        self.assertIn("Ctrl+Shift+2", result.stdout)
        # Defaults that Python did not override must survive the round trip too.
        self.assertIn("Ctrl+Alt+Left", result.stdout)
        self.assertIn("enabled: yes", result.stdout)
        for action in self.shortcuts.ACTIONS:
            self.assertIn(action, result.stdout)

    def test_list_refuses_symlinked_config(self):
        real = self.tmp / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = self.tmp / "config.json"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        result = self.run_cli("hotkeys", "list", path=link)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)

    def test_rust_init_waits_for_python_flock_then_preserves_modified_appearance(self):
        self.seed()
        lock_path = self.tmp / "config.json.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        released = False

        def release():
            nonlocal released
            if not released:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                released = True

        self.addCleanup(release)
        proc = subprocess.Popen(
            [str(self.binary), "hotkeys", "init", "--force"],
            cwd=ROOT,
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        try:
            # While the Python side holds the lock, the Rust reset must wait.
            time.sleep(0.6)
            self.assertIsNone(proc.poll(), "Rust init must wait for the Python flock")

            # A cooperating writer updates its own section under the same lock;
            # the waiting Rust reset must merge around it.
            data = json.loads(self.config.read_text(encoding="utf-8"))
            data["desktop"]["appearance"] = {
                "opacity": 0.55, "font_size": 22, "reduced_motion": False}
            data["added_while_locked"] = True
            temporary = self.tmp / "python-write.tmp"
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(temporary, self.config)
            release()
            _out, err = proc.communicate(timeout=30)
        finally:
            release()
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 0, err)
        final = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertIs(final["added_while_locked"], True)
        self.assertEqual(final["desktop"]["appearance"],
                         {"opacity": 0.55, "font_size": 22, "reduced_motion": False})
        self.assertEqual(final["desktop"]["window"], {"width": 480})
        self.assertIs(final["shortcuts"]["enabled"], True)
        self.assertEqual(final["shortcuts"]["bindings"]["start"], "ctrl+alt+s")
        self.assertEqual(final["unknown_top"], {"deep": True})


if __name__ == "__main__":
    unittest.main()
