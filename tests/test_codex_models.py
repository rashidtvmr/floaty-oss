"""Isolated JSON-RPC tests for Codex model discovery; no real account or inference."""
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from apps.socket_stream_desktop.ui.codex_models import (
    CodexModelsError,
    fetch_codex_models,
    validated_model,
)


class CodexModelDiscoveryTests(unittest.TestCase):
    def executable(self, body):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "fake-codex"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_handshake_and_model_list(self):
        path = self.executable('''
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if request.get("method") == "initialize":
                    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":{}}), flush=True)
                elif request.get("method") == "initialized":
                    pass
                elif request.get("method") == "model/list":
                    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":{
                        "data":[{"id":"catalog-entry", "model":"gpt-5-codex-v2", "displayName":"Codex v2"},
                                {"id":"legacy", "model":"legacy-2024-09"}], "nextCursor":None
                    }}), flush=True)
                    break
        ''')
        self.assertEqual(fetch_codex_models(str(path)), [
            {"id": "gpt-5-codex-v2", "name": "Codex v2"},
            {"id": "legacy-2024-09", "name": "legacy-2024-09"},
        ])

    def test_rpc_error_is_sanitized_and_child_is_reaped(self):
        path = self.executable('''
            import json, os, sys
            open(os.environ["FAKE_PID_FILE"], "w").write(str(os.getpid()))
            for line in sys.stdin:
                request = json.loads(line)
                if request.get("id") == 1:
                    print(json.dumps({"id":1, "result":{}}), flush=True)
                elif request.get("id") == 2:
                    print(json.dumps({"id":2, "error":{"code":-32000, "message":"secret token detail"}}), flush=True)
                    break
        ''')
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "pid"
            old = os.environ.get("FAKE_PID_FILE")
            os.environ["FAKE_PID_FILE"] = str(pid_file)
            try:
                with self.assertRaises(CodexModelsError) as caught:
                    fetch_codex_models(str(path))
            finally:
                if old is None:
                    os.environ.pop("FAKE_PID_FILE", None)
                else:
                    os.environ["FAKE_PID_FILE"] = old
            self.assertNotIn("secret token", str(caught.exception))
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_official_cursor_pages_use_inference_model_not_catalog_id(self):
        path = self.executable('''
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if request.get("id") == 1:
                    print(json.dumps({"id":1, "result":{}}), flush=True)
                elif request.get("method") == "model/list":
                    page = request["params"].get("cursor")
                    assert page in (None, "second-page")
                    print(json.dumps({"id":request["id"], "result":{
                        "data":[{"id":"catalog", "model":"model-2" if page else "model-1",
                                 "displayName":"Fixture"}],
                        "nextCursor":None if page else "second-page"}}), flush=True)
        ''')
        self.assertEqual([model["id"] for model in fetch_codex_models(str(path))],
                         ["model-1", "model-2"])

    def test_timeout_terminates_and_reaps_child(self):
        path = self.executable('''
            import json, os, sys, time
            open(os.environ["FAKE_PID_FILE"], "w").write(str(os.getpid()))
            for line in sys.stdin:
                request = json.loads(line)
                if request.get("id") == 1:
                    print(json.dumps({"id":1, "result":{}}), flush=True)
                elif request.get("id") == 2:
                    time.sleep(30)
        ''')
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "pid"
            old = os.environ.get("FAKE_PID_FILE")
            os.environ["FAKE_PID_FILE"] = str(pid_file)
            try:
                with self.assertRaisesRegex(CodexModelsError, "timed out"):
                    fetch_codex_models(str(path), timeout=0.3)
            finally:
                if old is None:
                    os.environ.pop("FAKE_PID_FILE", None)
                else:
                    os.environ["FAKE_PID_FILE"] = old
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_malformed_and_oversized_output_are_sanitized(self):
        for output in ("not-json\\n", "x" * (65 * 1024) + "\\n"):
            with self.subTest(size=len(output)):
                path = self.executable(f'''
                    import sys
                    sys.stdin.readline()
                    sys.stdout.write({output!r})
                    sys.stdout.flush()
                ''')
                with self.assertRaises(CodexModelsError):
                    fetch_codex_models(str(path), timeout=1)

    def test_unsupported_cli_start_is_sanitized(self):
        with self.assertRaisesRegex(CodexModelsError, "unavailable") as caught:
            fetch_codex_models("/not/a/real/codex")
        self.assertNotIn("/not/a/real/codex", str(caught.exception))

    def test_validated_model_rejects_blank_control_and_overlong_values(self):
        self.assertEqual(validated_model(" model-v2024-09 "), "model-v2024-09")
        for invalid in ("", "  ", "bad\nmodel", "x" * 129, "é" * 80, None):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValueError):
                validated_model(invalid)


if __name__ == "__main__":
    unittest.main()
