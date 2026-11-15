"""Tests for provider settings persistence (pure stdlib module)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import provider_settings as settings  # noqa: E402


class ProviderSettings(unittest.TestCase):
    def setUp(self):
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg

    def test_load_missing_returns_empty(self):
        self.assertEqual(settings.load_provider_settings(), {})

    def test_round_trip_with_user_only_permissions(self):
        env = {"SOCKET_STREAM_API_KEY": "sk-test", "SOCKET_STREAM_MODEL": "gpt-x"}
        path = settings.save_provider_settings(env)
        self.assertEqual(path, settings.provider_settings_path())
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        loaded = settings.load_provider_settings()
        self.assertEqual(loaded["SOCKET_STREAM_API_KEY"], "sk-test")
        self.assertEqual(loaded["SOCKET_STREAM_MODEL"], "gpt-x")
        # Every key round-trips, including empty ones, so clearing a field
        # in the dialog also clears the saved value on the next load.
        self.assertIn("SOCKET_STREAM_OLLAMA_URL", loaded)

    def test_save_replaces_file_atomically(self):
        settings.save_provider_settings({"SOCKET_STREAM_API_KEY": "first"})
        settings.save_provider_settings({"SOCKET_STREAM_API_KEY": "second"})
        self.assertEqual(
            settings.load_provider_settings()["SOCKET_STREAM_API_KEY"], "second"
        )
        leftovers = list(settings.provider_settings_path().parent.glob("*.part"))
        self.assertEqual(leftovers, [])

    def test_malformed_file_returns_empty(self):
        path = settings.provider_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(settings.load_provider_settings(), {})

    def test_non_dict_file_returns_empty(self):
        path = settings.provider_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(settings.load_provider_settings(), {})

    def test_clear_removes_file_and_reports_absent(self):
        settings.save_provider_settings({"SOCKET_STREAM_API_KEY": "sk-test"})
        self.assertTrue(settings.clear_provider_settings())
        self.assertFalse(settings.clear_provider_settings())
        self.assertEqual(settings.load_provider_settings(), {})


if __name__ == "__main__":
    unittest.main()
