#!/usr/bin/env python3
"""Local-only demonstration of browser display capture scope. No uploads."""
import argparse
import ipaddress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent / "web"


class LocalHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob: data:; media-src 'self' blob:; "
            "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'",
        )
        super().end_headers()

    def do_POST(self):
        self.send_error(405, "Local-only lab does not accept uploads")


def make_server(port=8765, host="127.0.0.1"):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("Bind only to a loopback address")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Invalid listening port")
    return ThreadingHTTPServer(
        (host, port), partial(LocalHandler, directory=str(SITE))
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(port=args.port)
    print(f"Capture Security Lab: http://127.0.0.1:{server.server_port}", flush=True)
    print("No recording or screen capture begins until you click Start in the browser.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
