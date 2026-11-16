"""Acceptance regression: manual vision survives the reader tempdir drop.

`ask --read-screen --authorized --force-ocr` captures a screenshot, reads the
question with OCR, and attaches the retained PNG to the OpenAI-compatible
request. The retained frame lives inside the screen reader's temporary
directory, which is removed when the reader is stopped/dropped. If the image
bytes are read from that path *after* the drop, the attachment silently
disappears and the model receives text only.

This suite runs the real Rust CLI with fake capture/accessibility/OCR helpers
and a loopback OpenAI-compatible endpoint, so it never touches a real screen,
the real accessibility stack, a real OCR engine, or the network. It skips
cleanly before the CLI is built and honours `CARGO_TARGET_DIR`.

Run with:

    python3 -m unittest discover -s tests/acceptance -p 'test_*.py' -v
"""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PNG = ROOT / "tests/fixtures/sample-code.png"
ANSWER = "Mock vision answer."

#: The accessibility helper reports this as the focused question. `--force-ocr`
#: makes the reader ignore it and OCR the captured frame instead, so the OCR
#: helper's output is what the model prompt must contain.
OCR_QUESTION = "What does this code do?"

A11Y_JSON = json.dumps({
    "schema": 1,
    "source": "atspi",
    "app": "SocketStream Fixture",
    "truncated": False,
    "nodes": [
        {"id": 1, "parent": None, "role": "frame", "name": "Fixture Window",
         "text": "", "focused": False},
        {"id": 2, "parent": 1, "role": "paragraph", "text": OCR_QUESTION,
         "focused": True},
    ],
})


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


def _write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class RecordingProvider:
    """Loopback OpenAI-compatible endpoint that records request bodies.

    A plain JSON completion is returned even though the CLI asks for a stream:
    the client accepts either shape, and a non-streamed reply keeps the fixture
    deterministic. Only request bodies are recorded; nothing is logged.
    """

    def __init__(self):
        self.requests = []
        record = self.requests

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # noqa: A003 - silence default logging
                return

            def _send(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                self._send(200, {"data": [{"id": "mock-model"}]})

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    record.append(json.loads(raw.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    record.append({"raw": raw.decode("utf-8", "replace")})
                self._send(200, {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": "mock-model",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }],
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ManualVisionEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = locate_cli()
        if cls.binary is None:
            raise unittest.SkipTest(
                "socket_stream is not built; run cargo build -p socket_stream")
        if not FIXTURE_PNG.is_file():
            raise unittest.SkipTest(f"missing fixture: {FIXTURE_PNG}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.fake_bin = base / "bin"
        # A private TMPDIR lets us prove the reader's capture files are gone.
        self.tmpdir = base / "tmp"
        for directory in (self.home, self.fake_bin, self.tmpdir):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = base / "config.json"
        self.png_bytes = FIXTURE_PNG.read_bytes()
        self._install_helpers()
        self.provider = RecordingProvider()
        self.addCleanup(self.provider.close)

    # ---- fixture -----------------------------------------------------------

    def _copy_script(self):
        """A capture stub that copies the PNG fixture to its last argument."""
        return ("#!/bin/sh\n"
                "for last; do :; done\n"
                f"cp {shlex.quote(str(FIXTURE_PNG))} \"$last\"\n")

    def _ocr_script(self):
        """Print the question and record the frame path the reader handed us."""
        return ("#!/bin/sh\n"
                f"printf '%s' \"$1\" > {shlex.quote(str(self.ocr_arg))}\n"
                f"cat {shlex.quote(str(self.ocr_file))}\n")

    def _install_helpers(self):
        self.tree_file = self.home / "tree.json"
        self.tree_file.write_text(A11Y_JSON, encoding="utf-8")
        self.ocr_file = self.home / "ocr.txt"
        self.ocr_file.write_text(OCR_QUESTION, encoding="utf-8")
        #: Records the captured-frame path the OCR helper was invoked with.
        self.ocr_arg = self.home / "ocr-arg.txt"

        if sys.platform == "darwin":
            helpers = self.home / "target/native-helpers"
            _write_executable(
                helpers / "socket_stream_a11y",
                f"#!/bin/sh\ncat {shlex.quote(str(self.tree_file))}\n")
            _write_executable(helpers / "socket_stream_ocr", self._ocr_script())
            _write_executable(self.fake_bin / "screencapture", self._copy_script())
        else:
            script = ("#!/usr/bin/env python3\n"
                      "import sys\n"
                      f"sys.stdout.write(open({str(self.tree_file)!r}, encoding='utf-8').read())\n")
            _write_executable(self.home / "scripts/linux-accessibility.py", script)
            _write_executable(self.fake_bin / "tesseract", self._ocr_script())
            _write_executable(self.fake_bin / "import", self._copy_script())

    # ---- environment -------------------------------------------------------

    def env(self):
        env = os.environ.copy()
        env["SOCKET_STREAM_HOME"] = str(self.home)
        # Isolate configuration: a missing file yields defaults, never the
        # developer's real config or provider settings.
        env["SOCKET_STREAM_CONFIG"] = str(self.config)
        env["SOCKET_STREAM_BASE_URL"] = f"http://127.0.0.1:{self.provider.port}/v1"
        env["SOCKET_STREAM_MODEL"] = "mock-model"
        env["SOCKET_STREAM_API_KEY"] = ""
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TMPDIR"] = str(self.tmpdir)
        # Never route accessibility through the native app's signed executable.
        env.pop("SOCKET_STREAM_MACOS_A11Y_EXECUTABLE", None)
        if sys.platform.startswith("linux"):
            # Force the X11 capture path and avoid the ScreenCast portal.
            env["DISPLAY"] = ":99"
            env.pop("WAYLAND_DISPLAY", None)
            env.pop("XDG_CURRENT_DESKTOP", None)
        return env

    def run_cli(self, *args, timeout=60):
        return subprocess.run(
            [str(self.binary), *args],
            cwd=ROOT,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def ask_args(self, *extra):
        return ("ask", "--read-screen", "--authorized", "--force-ocr",
                "--provider", "openai-compatible", *extra)

    def user_message(self, request):
        messages = request.get("messages", [])
        users = [message for message in messages if message.get("role") == "user"]
        self.assertTrue(users, f"no user message in recorded request: {request}")
        return users[-1]

    # ---- tests -------------------------------------------------------------

    def test_read_screen_attaches_the_captured_png_to_the_model_request(self):
        result = self.run_cli(*self.ask_args())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.provider.requests), 1, self.provider.requests)

        request = self.provider.requests[0]
        self.assertIs(request.get("stream"), True, request)
        self.assertEqual(request.get("model"), "mock-model")

        user = self.user_message(request)
        self.assertIsInstance(
            user["content"], list,
            "vision request must use OpenAI content parts, got: "
            f"{user['content']!r}\nstdout={result.stdout}\nstderr={result.stderr}")

        image_parts = [part for part in user["content"]
                       if part.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 1, user["content"])
        url = image_parts[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"), url[:64])

        encoded = url.split(",", 1)[1]
        self.assertEqual(
            base64.b64decode(encoded), self.png_bytes,
            "the attached image must be the captured frame, byte for byte")

        text_parts = [part for part in user["content"]
                      if part.get("type") == "text"]
        self.assertTrue(text_parts, user["content"])
        self.assertIn(OCR_QUESTION, text_parts[0]["text"])

    def test_no_vision_sends_plain_text_without_an_image(self):
        result = self.run_cli(*self.ask_args("--no-vision"))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.provider.requests), 1, self.provider.requests)

        request = self.provider.requests[0]
        user = self.user_message(request)
        self.assertIsInstance(user["content"], str, user["content"])
        self.assertIn(OCR_QUESTION, user["content"])
        self.assertNotIn("data:image/png", json.dumps(request))

    def test_capture_frame_is_created_under_tmpdir_and_cleaned_up(self):
        result = self.run_cli(*self.ask_args())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        # The OCR helper must have been handed a frame captured under our
        # private TMPDIR: proves the capture actually ran and where it landed.
        self.assertTrue(self.ocr_arg.is_file(),
                        "OCR helper never received a captured frame")
        captured = self.ocr_arg.read_text(encoding="utf-8")
        self.assertTrue(captured.startswith(str(self.tmpdir)),
                        f"capture frame not under TMPDIR: {captured!r}")

        # ...and the reader's temporary directory must be gone afterwards.
        leftovers = sorted(
            str(path.relative_to(self.tmpdir))
            for path in self.tmpdir.rglob("*")
            if path.is_file())
        self.assertEqual(leftovers, [], "reader temp files must be cleaned up")


if __name__ == "__main__":
    unittest.main()
