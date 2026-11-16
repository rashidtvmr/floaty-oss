import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from provider_catalog import fetch_models
from codex_models import CodexModelsError
from shortcuts import load_appearance, save_appearance


class ProviderCatalogTests(unittest.TestCase):
    def test_discovery_is_non_inference_and_key_is_only_in_child_environment(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="Connection and model-list request succeeded; no inference was requested.\nmodel-a\nmodel-a\nmodel-b\n")
        result = fetch_models("openai-compatible", "/fixture/cli", {"SOCKET_STREAM_API_KEY": "fixture-secret"}, run=run)
        self.assertEqual([m["id"] for m in result], ["model-a", "model-b"])
        self.assertNotIn("fixture-secret", str(calls[0][0]))
        self.assertEqual(calls[0][1]["env"]["SOCKET_STREAM_API_KEY"], "fixture-secret")
        self.assertNotIn("provider-test", calls[0][0])

    def test_discovery_errors_do_not_expose_output(self):
        for reply in (SimpleNamespace(returncode=1, stdout="fixture-secret"),
                      SimpleNamespace(returncode=0, stdout="malformed fixture-secret")):
            with self.assertRaises(CodexModelsError) as seen:
                fetch_models("ollama", "/fixture/cli", {}, run=lambda *a, **k: reply)
            self.assertNotIn("fixture-secret", str(seen.exception))

    def test_codex_uses_official_catalog_and_mock_does_not_call_network(self):
        with patch("provider_catalog.fetch_codex_models", return_value=[{"id": "official"}]) as fetch:
            self.assertEqual(fetch_models("codex", "unused", {}), [{"id": "official"}])
            fetch.assert_called_once_with()
        self.assertEqual(fetch_models("mock", "unused", {}, run=lambda *a, **k: self.fail()), [])

    def test_theme_round_trip_and_invalid_values_preserve_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            values = load_appearance(path)
            for theme in ("dark", "light", "system"):
                save_appearance(dict(values, theme=theme), path=path)
                self.assertEqual(load_appearance(path)["theme"], theme)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                save_appearance(dict(values, theme="invalid"), path=path)
            self.assertEqual(path.read_bytes(), before)
