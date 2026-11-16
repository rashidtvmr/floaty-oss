"""Tests for provider settings persistence (pure stdlib module)."""
import json
import os
import sys
import tempfile
import unittest
import fcntl
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

    def test_named_profiles_preserve_legacy_credentials_and_codex_model(self):
        settings.save_provider_settings({"SOCKET_STREAM_API_KEY": "legacy-secret", "SOCKET_STREAM_MODEL": "legacy-model"})
        settings.save_provider_profile("Codex", "codex", {"SOCKET_STREAM_CODEX_MODEL": "preferred-codex"})
        book = settings.load_provider_profiles()
        self.assertEqual(book["profiles"]["Default"]["settings"]["SOCKET_STREAM_API_KEY"], "legacy-secret")
        self.assertEqual(book["profiles"]["Codex"]["settings"]["SOCKET_STREAM_CODEX_MODEL"], "preferred-codex")
        settings.save_provider_profile("Second cloud", "openai-compatible", {"SOCKET_STREAM_API_KEY": "second-secret", "SOCKET_STREAM_MODEL": "second-model"}, "Codex")
        self.assertEqual(settings.load_provider_settings()["SOCKET_STREAM_API_KEY"], "second-secret")
        self.assertEqual(settings.load_provider_profiles()["fallback_profile"], "Codex")
        settings.save_provider_profile("Second cloud", "openai-compatible", {"SOCKET_STREAM_MODEL": "second-model"})
        self.assertEqual(settings.load_provider_profiles()["fallback_profile"], "")
        self.assertEqual(settings.provider_settings_path().stat().st_mode & 0o777, 0o600)

    def test_fallback_requires_a_different_saved_profile(self):
        settings.save_provider_profile("Primary", "codex", {})
        before = settings.provider_settings_path().read_bytes()
        for fallback in ("Primary", "not-saved"):
            with self.assertRaises(ValueError):
                settings.save_provider_profile("Primary", "codex", {}, fallback)
            self.assertEqual(settings.provider_settings_path().read_bytes(), before)

    def test_delete_one_preserves_others_and_clears_invalid_fallback(self):
        settings.save_provider_profile("A", "codex", {"SOCKET_STREAM_CODEX_MODEL": "a"})
        settings.save_provider_profile("B", "openai-compatible", {"SOCKET_STREAM_API_KEY": "fixture-b"}, "A")
        settings.delete_provider_profile("B")
        book = settings.load_provider_profiles()
        self.assertEqual(list(book["profiles"]), ["A"])
        self.assertEqual(book["active_profile"], "A")
        self.assertEqual(book["fallback_profile"], "")
        self.assertEqual(settings.load_provider_settings()["SOCKET_STREAM_CODEX_MODEL"], "a")
        self.assertEqual(settings.load_provider_settings()["SOCKET_STREAM_API_KEY"], "")
        settings.delete_provider_profile("A")
        self.assertEqual(settings.load_provider_profiles()["profiles"], {})
        self.assertEqual(settings.load_provider_settings(), {})

    def test_rename_preserves_credentials_and_fallback_identity(self):
        settings.save_provider_profile("Backup", "codex", {"SOCKET_STREAM_CODEX_MODEL": "backup-model"})
        settings.save_provider_profile("Primary", "openai-compatible", {"SOCKET_STREAM_API_KEY": "fixture-p"}, "Backup")
        settings.rename_provider_profile("Backup", "Renamed")
        book = settings.load_provider_profiles()
        self.assertEqual(book["fallback_profile"], "Renamed")
        self.assertEqual(book["profiles"]["Renamed"]["settings"]["SOCKET_STREAM_CODEX_MODEL"], "backup-model")
        settings.rename_provider_profile("Primary", "Cloud")
        self.assertEqual(settings.load_provider_profiles()["active_profile"], "Cloud")
        self.assertEqual(settings.load_provider_settings()["SOCKET_STREAM_API_KEY"], "fixture-p")
        self.assertEqual(settings.provider_settings_path().stat().st_mode & 0o777, 0o600)

    def test_profile_mutation_failures_preserve_file(self):
        settings.save_provider_profile("A", "codex", {})
        settings.save_provider_profile("B", "codex", {})
        before = settings.provider_settings_path().read_bytes()
        for action in (lambda: settings.rename_provider_profile("A", "B"),
                       lambda: settings.rename_provider_profile("A", "\n"),
                       lambda: settings.delete_provider_profile("absent")):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(settings.provider_settings_path().read_bytes(), before)

    def test_symlinks_and_malformed_settings_are_preserved_not_overwritten(self):
        path = settings.provider_settings_path()
        path.parent.mkdir(parents=True)
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text('{"keep":"untouched"}')
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            settings.save_provider_profile("Unsafe", "codex", {})
        self.assertEqual(outside.read_text(), '{"keep":"untouched"}')
        path.unlink()
        path.write_text("malformed")
        with self.assertRaises(ValueError):
            settings.save_provider_profile("Unsafe", "codex", {})
        self.assertEqual(path.read_text(), "malformed")

    def test_profile_lock_refuses_concurrent_writes_without_waiting(self):
        settings.save_provider_profile("First", "codex", {})
        lock = settings.provider_settings_path().parent / ".provider-settings.lock"
        with lock.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                settings.save_provider_profile("Second", "codex", {})
            with self.assertRaises(BlockingIOError):
                settings.clear_provider_settings()
            self.assertTrue(settings.provider_settings_path().is_file())
        self.assertTrue(settings.clear_provider_settings())

    def test_insecure_files_and_directories_are_not_used_for_credentials(self):
        path = settings.save_provider_settings({"SOCKET_STREAM_API_KEY": "private-secret"})
        path.chmod(0o644)
        self.assertEqual(settings.load_provider_settings(), {})
        with self.assertRaises(ValueError):
            settings.save_provider_profile("New", "codex", {})
        path.chmod(0o600)
        path.parent.chmod(0o777)
        try:
            self.assertEqual(settings.load_provider_settings(), {})
            with self.assertRaises(ValueError):
                settings.save_provider_profile("New", "codex", {})
        finally:
            path.parent.chmod(0o700)

    def test_malformed_profile_metadata_is_preserved(self):
        path = settings.save_provider_settings({})
        cases = [{"SOCKET_STREAM_API_KEY": ["invalid-secret"]}, {"profiles": []}, {"profiles": {"Broken": {"provider": "unknown", "settings": {}}}},
                 {"profiles": {}, "active_profile": []}, {"fallback_profile": {}},
                 {"profiles": {}, "fallback_profile": "missing"}]
        for data in cases:
            before = json.dumps(data).encode()
            path.write_bytes(before)
            self.assertEqual(settings.load_provider_profiles()["profiles"], {})
            with self.assertRaises(ValueError):
                settings.save_provider_profile("New", "codex", {})
            self.assertEqual(path.read_bytes(), before)

    def test_excessively_nested_json_is_rejected_and_preserved(self):
        path = settings.save_provider_settings({})
        before = ('{"unknown":' + '[' * 2000 + '0' + ']' * 2000 + '}').encode()
        path.write_bytes(before)
        self.assertEqual(settings.load_provider_settings(), {})
        with self.assertRaises(ValueError):
            settings.save_provider_profile("New", "codex", {})
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
