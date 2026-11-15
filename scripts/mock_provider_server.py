#!/usr/bin/env python3
"""Small local OpenAI-compatible provider for deterministic integration tests.

This server is intended for loopback-only development and acceptance tests. It
never logs request bodies and rejects request payloads larger than 1 MiB.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import ipaddress
import json
import socket
import sys
import time

MAX_REQUEST_BYTES = 1024 * 1024


def _loopback_bind_target(host, port):
    """Resolve an allowed host to a numeric loopback address before binding."""
    if host == "localhost":
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ValueError("mock provider must bind to loopback") from error
        for family, _socktype, _protocol, _canonname, sockaddr in addresses:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if address.is_loopback:
                return family, sockaddr[0]
        raise ValueError("mock provider must bind to loopback")

    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("mock provider must bind to loopback") from error
    if not address.is_loopback:
        raise ValueError("mock provider must bind to loopback")
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    return family, host


def make_handler(model="mock-model", answer="Mock provider answer.", delay_ms=0, fail_mode="none"):
    """Return a configured, import-safe HTTP request handler class."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            # Deliberately suppress default logging (which can expose paths and
            # client details); request bodies are never logged.
            return

        def _headers(self, status, content_type="application/json", length=0, chunked=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(length))
            self.send_header("Connection", "close")
            self.end_headers()

        def do_GET(self):
            if self.path.rstrip("/") != "/v1/models":
                body = b'{"error":"not found"}'
                self._headers(404, length=len(body))
                self.wfile.write(body)
                return
            if fail_mode == "http500":
                body = b'{"error":"mock failure"}'
                self._headers(500, length=len(body))
                self.wfile.write(body)
                return
            if fail_mode == "empty":
                self._headers(200, length=0)
                return
            body = json.dumps({"data": [{"id": model}]}).encode("utf-8")
            self._headers(200, length=len(body))
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/chat/completions":
                body = b'{"error":"not found"}'
                self._headers(404, length=len(body))
                self.wfile.write(body)
                return
            try:
                content_lengths = self.headers.get_all("Content-Length", [])
                if len(content_lengths) > 1:
                    body = b'{"error":"invalid content length"}'
                    self._headers(400, length=len(body))
                    self.wfile.write(body)
                    return
                length = int(content_lengths[0]) if content_lengths else 0
            except ValueError:
                body = b'{"error":"invalid content length"}'
                self._headers(400, length=len(body))
                self.wfile.write(body)
                return
            if length < 0:
                body = b'{"error":"invalid content length"}'
                self._headers(400, length=len(body))
                self.wfile.write(body)
                return
            if length > MAX_REQUEST_BYTES:
                body = b'{"error":"request too large"}'
                self._headers(413, length=len(body))
                self.wfile.write(body)
                return
            request_body = self.rfile.read(length)
            if len(request_body) != length:
                body = b'{"error":"incomplete request"}'
                self._headers(400, length=len(body))
                self.wfile.write(body)
                return
            if fail_mode == "timeout":
                # Test-only latency; close after the delay rather than returning
                # a misleading successful inference response.
                time.sleep(max(0, delay_ms) / 1000)
                self.close_connection = True
                return
            if fail_mode == "http500":
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                body = b'{"error":"mock failure"}'
                self._headers(500, length=len(body))
                self.wfile.write(body)
                return
            if fail_mode == "empty":
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                self._headers(200, length=0)
                return

            try:
                payload = json.loads(request_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = b'{"error":"invalid json"}'
                self._headers(400, length=len(body))
                self.wfile.write(body)
                return
            if not isinstance(payload, dict):
                body = b'{"error":"request must be a JSON object"}'
                self._headers(400, length=len(body))
                self.wfile.write(body)
                return
            if payload.get("stream") is True:
                self._write_stream(delay_ms)
            else:
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                self._write_completion()

        def _write_completion(self):
            body = json.dumps({
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }],
            }).encode("utf-8")
            self._headers(200, length=len(body))
            self.wfile.write(body)

        def _write_stream(self, stream_delay_ms=0):
            created = int(time.time())
            def deltas():
                yield {"role": "assistant"}
                for index in range(0, len(answer), 24):
                    yield {"content": answer[index:index + 24]}
                yield {}

            self._headers(200, "text/event-stream", chunked=True)
            delta_events = deltas()
            try:
                for delta in delta_events:
                    event = {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": delta,
                            "finish_reason": "stop" if not delta else None,
                        }],
                    }
                    frame = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(f"{len(frame):X}\r\n".encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    if stream_delay_ms > 0:
                        time.sleep(stream_delay_ms / 1000)

                done = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(done):X}\r\n".encode("ascii"))
                self.wfile.write(done)
                self.wfile.write(b"\r\n0\r\n\r\n")
                self.wfile.flush()
            except ConnectionError:
                # A consumer may cancel a stream early; that is not a server error.
                self.close_connection = True

    return Handler


def make_server(host="127.0.0.1", port=0, model="mock-model", answer="Mock provider answer.",
                delay_ms=0, fail_mode="none"):
    """Create but do not start a configured server (useful to tests)."""
    family, bind_host = _loopback_bind_target(host, port)
    handler = make_handler(model, answer, delay_ms, fail_mode)
    if family == socket.AF_INET6:
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_type = IPv6ThreadingHTTPServer
    else:
        server_type = ThreadingHTTPServer
    server = server_type((bind_host, port), handler)
    try:
        bound_host = ipaddress.ip_address(server.server_address[0])
    except ValueError:
        server.server_close()
        raise ValueError("mock provider must bind to a loopback address")
    if not bound_host.is_loopback:
        server.server_close()
        raise ValueError("mock provider must bind to a loopback address")
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--fail-mode", choices=("none", "http500", "empty", "timeout"), default="none")
    parser.add_argument("--answer", default="Mock provider answer.")
    args = parser.parse_args(argv)
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be between 0 and 65535")
    if args.delay_ms < 0:
        parser.error("--delay-ms cannot be negative")
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        parser.error("mock provider must bind to loopback")
    server = make_server(args.host, args.port, args.model, args.answer, args.delay_ms, args.fail_mode)
    print(f"PORT={server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
