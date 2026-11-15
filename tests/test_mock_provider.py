"""Loopback-only tests for the stdlib OpenAI-compatible mock server."""
import importlib.util
import http.client
import ipaddress
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mock_provider_server.py"
spec = importlib.util.spec_from_file_location("mock_provider_server", SCRIPT)
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)


class ServerCase(unittest.TestCase):
    def start_server(self, **options):
        server = mock_server.make_server(**options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def request(url, data=None, headers=None):
        request = urllib.request.Request(url, data=data, headers=headers or {})
        return urllib.request.urlopen(request, timeout=5)

    def test_models_endpoint_is_openai_compatible(self):
        base = self.start_server(model="mock-model")
        with self.request(base + "/v1/models") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload, {"data": [{"id": "mock-model"}]})

    def test_non_streaming_completion(self):
        base = self.start_server(answer="Use three pointers.")
        body = json.dumps({"model": "mock-model", "stream": False}).encode()
        with self.request(base + "/v1/chat/completions", body, {"Content-Type": "application/json"}) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["choices"][0]["message"]["content"], "Use three pointers.")

    def test_streaming_completion_yields_ordered_deltas(self):
        base = self.start_server(answer="abcdefghijklmnop")
        body = json.dumps({"model": "mock-model", "stream": True}).encode()
        with self.request(base + "/v1/chat/completions", body, {"Content-Type": "application/json"}) as response:
            self.assertEqual(response.headers.get("Transfer-Encoding"), "chunked")
            self.assertIsNone(response.headers.get("Content-Length"))
            wire = response.read().decode()
        deltas = []
        for line in wire.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                event = json.loads(line[6:])
                deltas.append(event["choices"][0]["delta"].get("content", ""))
        self.assertEqual("".join(deltas), "abcdefghijklmnop")
        self.assertTrue(wire.rstrip().endswith("data: [DONE]"))

    def test_stream_frames_are_flushed_incrementally(self):
        base = self.start_server(answer="one two", delay_ms=40)
        connection = http.client.HTTPConnection("127.0.0.1", int(base.rsplit(":", 1)[1]), timeout=5)
        self.addCleanup(connection.close)
        body = json.dumps({"model": "mock-model", "stream": True}).encode()
        connection.request(
            "POST", "/v1/chat/completions", body,
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertIsNone(response.getheader("Content-Length"))

        first_data = response.readline()
        self.assertTrue(first_data.startswith(b"data: "))
        self.assertEqual(response.readline(), b"\n")
        first_frame_at = time.monotonic()
        second_data = response.readline()
        self.assertTrue(second_data.startswith(b"data: "))
        self.assertGreaterEqual(time.monotonic() - first_frame_at, 0.02)
        first_event = json.loads(first_data[6:])
        second_event = json.loads(second_data[6:])
        self.assertEqual(first_event["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(second_event["choices"][0]["delta"], {"content": "one two"})
        while response.readline():
            pass

    def test_http500_failure_mode(self):
        base = self.start_server(fail_mode="http500")
        body = b'{"model":"mock-model"}'
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(base + "/v1/chat/completions", body)
        self.assertEqual(raised.exception.code, 500)

    def test_empty_failure_mode(self):
        base = self.start_server(fail_mode="empty")
        body = b'{"model":"mock-model"}'
        with self.request(base + "/v1/chat/completions", body) as response:
            self.assertEqual(response.read(), b"")

    def test_timeout_failure_mode_closes_without_a_completion(self):
        base = self.start_server(fail_mode="timeout", delay_ms=10)
        body = b'{"model":"mock-model"}'
        with self.assertRaises((urllib.error.URLError, http.client.RemoteDisconnected)):
            self.request(base + "/v1/chat/completions", body)

    def test_non_object_json_is_rejected_cleanly(self):
        base = self.start_server()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(base + "/v1/chat/completions", b"[]")
        self.assertEqual(raised.exception.code, 400)

    def test_oversized_body_is_refused(self):
        base = self.start_server()
        connection = http.client.HTTPConnection("127.0.0.1", int(base.rsplit(":", 1)[1]), timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(mock_server.MAX_REQUEST_BYTES + 1))
        # Deliberately do not transmit the body: a conforming server can reject
        # from Content-Length without buffering or waiting for an oversized upload.
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn(b"too large", response.read())

    def test_body_at_size_limit_is_not_rejected_as_oversized(self):
        base = self.start_server()
        body = b"x" * mock_server.MAX_REQUEST_BYTES
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(base + "/v1/chat/completions", body)
        self.assertEqual(raised.exception.code, 400)

    def test_server_refuses_non_loopback_bind(self):
        for host in ("0.0.0.0", "192.0.2.1", "provider.example"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "loopback"):
                mock_server.make_server(host=host)

    def test_localhost_is_resolved_to_a_numeric_loopback_bind(self):
        server = mock_server.make_server(host="localhost")
        try:
            self.assertTrue(ipaddress.ip_address(server.server_address[0]).is_loopback)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
