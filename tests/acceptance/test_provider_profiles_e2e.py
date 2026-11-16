"""Real CLI named fallback configuration; loopback fixtures, no account/media."""
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]


class ProfileFallbackE2E(unittest.TestCase):
    def setUp(self):
        target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
        self.binary = next((p for p in (target / "debug/socket_stream", ROOT / "bin/socket_stream") if p.is_file()), None)
        if self.binary is None:
            self.skipTest("build CLI first")
        self.posts = []
        self.primary = self.server(False)
        self.backup = self.server(True)
        self.env = dict(os.environ, SOCKET_STREAM_BASE_URL=self.primary, SOCKET_STREAM_MODEL="primary-model",
                        SOCKET_STREAM_API_KEY="primary-fixture-key", SOCKET_STREAM_FALLBACK_CONFIG=json.dumps({
                            "provider": "openai-compatible", "settings": {
                                "SOCKET_STREAM_BASE_URL": self.backup, "SOCKET_STREAM_MODEL": "backup-model",
                                "SOCKET_STREAM_API_KEY": "backup-fixture-key"}}))

    def server(self, backup):
        posts = self.posts

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send(200, {"data": [{"id": "backup-model" if backup else "primary-model"}]})

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append((backup, data["model"], self.headers.get("Authorization")))
                if backup:
                    self.send(200, {"choices": [{"message": {"content": "A short fallback answer."}}]})
                else:
                    self.send(503, {"error": "fixture unavailable"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}/v1"

    def ask(self, *options):
        return subprocess.run([str(self.binary), "ask", "--question", "Explain binary search briefly.",
                               "--provider", "openai-compatible", *options], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_fallback_is_not_enabled_by_an_environment_variable_alone(self):
        result = self.ask()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.posts)
        self.assertFalse(any(backup for backup, _, _ in self.posts))

    def test_opt_in_fallback_uses_its_own_endpoint_model_and_key(self):
        result = self.ask("--use-profile-fallback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A short fallback answer.", result.stdout)
        self.assertEqual(self.posts, [(False, "primary-model", "Bearer primary-fixture-key"),
                                      (True, "backup-model", "Bearer backup-fixture-key")])
        self.assertNotIn("fixture-key", result.stdout + result.stderr)

    def test_provider_test_cannot_conceal_primary_failure_with_fallback(self):
        result = subprocess.run([str(self.binary), "provider-test", "--provider", "openai-compatible",
                                 "--confirm-inference", "--use-profile-fallback"], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one provider at a time", result.stderr + result.stdout)
        self.assertFalse(self.posts)
