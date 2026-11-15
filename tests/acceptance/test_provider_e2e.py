"""Offline provider E2E against the local OpenAI-compatible mock server.

The suite exercises the real Rust CLI binary and skips cleanly before it is
built. It never contacts a remote provider or uses real credentials.
"""
import os
from pathlib import Path
import http.client
import json
import subprocess
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI_CANDIDATES = (
    ROOT / "target/debug/socket_stream",
    ROOT / "target/debug/bin/socket_stream",
    ROOT / "bin/socket_stream",
)
QUESTION = ROOT / "tests/fixtures/mock_provider/questions.txt"


def locate_cli():
    return next((path for path in CLI_CANDIDATES if path.is_file()), None)


class ProviderEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = locate_cli()
        if cls.binary is None:
            raise unittest.SkipTest("socket_stream is not built; run cargo build -p socket_stream")
        import importlib.util

        script = ROOT / "scripts/mock_provider_server.py"
        spec = importlib.util.spec_from_file_location("e2e_mock_provider", script)
        cls.mock = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mock)

    def start_server(self, **options):
        server = self.mock.make_server(**options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def run_cli(self, *args, port=None):
        env = os.environ.copy()
        if port is not None:
            env["SOCKET_STREAM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
            env["SOCKET_STREAM_MODEL"] = "mock-model"
            env["SOCKET_STREAM_API_KEY"] = ""
        return subprocess.run(
            [str(self.binary), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_provider_models_lists_mock_model(self):
        port = self.start_server(model="mock-model")
        result = self.run_cli("provider-models", "--provider", "openai-compatible", port=port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mock-model", result.stdout)

    def test_provider_test_performs_confirmed_inference(self):
        port = self.start_server(answer="READY")
        result = self.run_cli(
            "provider-test", "--provider", "openai-compatible", "--confirm-inference", port=port
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Inference test succeeded", result.stdout)

    def test_analyze_file_displays_mock_answer(self):
        port = self.start_server(answer="Use previous, current, and next pointers.")
        result = self.run_cli(
            "analyze-file", "--file", str(QUESTION), "--provider", "openai-compatible", port=port
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Use previous, current, and next pointers.", result.stdout)

    def test_mock_stream_emits_ordered_chunked_sse_deltas(self):
        port = self.start_server(answer="first second", delay_ms=5)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(connection.close)
        body = json.dumps({"model": "mock-model", "stream": True}).encode()
        connection.request(
            "POST", "/v1/chat/completions", body,
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertIsNone(response.getheader("Content-Length"))

        events = []
        while True:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data: ") and line != b"data: [DONE]\n":
                events.append(json.loads(line[6:]))
        deltas = [event["choices"][0]["delta"] for event in events]
        self.assertEqual(deltas[0], {"role": "assistant"})
        self.assertEqual(
            [delta["content"] for delta in deltas if "content" in delta],
            ["first second"],
        )
        self.assertEqual(deltas[-1], {})
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def test_closed_port_fails_with_clear_provider_error(self):
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        result = self.run_cli("provider-models", "--provider", "openai-compatible", port=port)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            "cannot reach" in result.stderr.lower() or "cannot reach" in result.stdout.lower(),
            result.stderr + result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
