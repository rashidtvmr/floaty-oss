"""Real CLI turn gating with synthetic STT/capture, no devices or remote AI."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]


class SpeechTurnsE2E(unittest.TestCase):
    def setUp(self):
        target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
        self.binary = next((p for p in (target / "debug/socket_stream", ROOT / "bin/socket_stream")
                            if p.is_file()), None)
        if self.binary is None:
            self.skipTest("build the CLI first")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        self.commands = self.root / "speech.jsonl"
        self.commands.write_text("")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        helpers = self.root / "target/native-helpers"
        worker = helpers / "moonshine/socket_stream_moonshine/socket_stream_moonshine"
        worker.parent.mkdir(parents=True)
        worker.write_text(f'''#!{sys.executable}
import json, pathlib, time
commands = pathlib.Path({str(self.commands)!r})
print(json.dumps({{"type": "ready", "text": ""}}), flush=True)
seen = 0
while True:
    lines = commands.read_text().splitlines()
    for line in lines[seen:]:
        print(line, flush=True)
    seen = len(lines)
    time.sleep(0.02)
''')
        worker.chmod(0o700)
        # Never execute the platform capture tools: override the exact capture
        # entry points. The fake speech worker emits test-controlled final text.
        for capture in (helpers / "socket_stream_audio_stream", self.fake_bin / "ffmpeg"):
            capture.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
            capture.chmod(0o700)

    def launch(self, role, read_screen=False, provider="mock", extra_env=None, screen_only=False, extra_args=()):
        env = dict(os.environ, SOCKET_STREAM_HOME=str(self.root),
                   SOCKET_STREAM_CONFIG=str(self.root / "missing-config.json"),
                    PATH=str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        env.update(extra_env or {})
        args = [str(self.binary), "audio-live", "--authorized", "--source", "microphone",
                "--speaker-role", role, "--provider", provider]
        if screen_only:
            args = [str(self.binary), "observe-live", "--authorized", "--provider", provider, "--interval", "2"]
        if read_screen:
            args.extend(["--read-screen", "--read-interval", "2"])
        args.extend(extra_args)
        self.proc = subprocess.Popen(
            args,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup)
        self.events = queue.Queue()

        def read():
            for line in self.proc.stdout:
                self.events.put(json.loads(line))
            self.events.put(None)

        threading.Thread(target=read, daemon=True).start()
        self.until("session_started")

    def cleanup(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()

    def command(self, text):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def speech(self, text):
        with self.commands.open("a") as handle:
            handle.write(json.dumps({"type": "final", "text": text}) + "\n")

    def until(self, kind):
        seen = []
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, f"no {kind}")
            event = self.events.get(timeout=remaining)
            self.assertIsNotNone(event, "CLI exited early")
            self.assertNotEqual(event["type"], "error", event.get("message"))
            seen.append(event)
            if event["type"] == kind:
                return seen

    def assert_no_automatic_question(self):
        deadline = time.monotonic() + 1.3  # production debounce is 900 ms
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.assertIsNotNone(event)
            self.assertNotIn(event["type"], ("question_detected", "answer_started", "error"), event)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux AT-SPI fixed-window boundary")
    def test_missing_selected_window_never_falls_back_to_pixels(self):
        helper = self.root / "scripts/linux-accessibility.py"
        helper.write_text("import sys\nprint('Selected question window is unavailable', file=sys.stderr)\nsys.exit(3)\n")
        marker = self.root / "pixel-capture-invoked"
        for tool in ("gdbus", "grim", "gnome-screenshot", "scrot", "tesseract"):
            path = self.fake_bin / tool
            path.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n")
            path.chmod(0o700)
        self.launch("practice", screen_only=True,
                    extra_env={"SOCKET_STREAM_ATSPI_TARGET": '[":1.99","/window/1"]'},
                    extra_args=("--force-ocr", "--portal", "--ocr-images"))
        events = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            event = self.events.get(timeout=max(0.001, deadline - time.monotonic()))
            self.assertIsNotNone(event, "CLI exited before reporting the unavailable target")
            events.append(event)
            self.assertNotIn(event["type"], ("question_detected", "answer_started"))
            if event["type"] == "error":
                break
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(marker.exists(), "fixed-window consent must not invoke pixel tools")
        self.assertTrue(any("Selected question window" in json.dumps(event) for event in events))

    def stop(self):
        self.command("stop")
        self.until("session_stopped")
        self.assertEqual(self.proc.wait(timeout=5), 0)

    def test_own_answer_is_suppressed_but_explicit_speech_clarification_refines(self):
        self.launch("interviewee")
        self.command("question How does binary search work?")
        self.until("answer_completed")
        self.speech("I would halve the search interval. Why not scan? That is slower.")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.speech("So you mean the input needs to be sorted?")
        events = self.until("answer_completed")
        text = "".join(event.get("text", "") for event in events if event["type"] == "answer_delta")
        # The mock receives acknowledged history separately from the latest
        # clarification; the previous question need not be duplicated in it.
        self.assertIn("How does binary search work?", text)
        self.assertIn("Latest explicit clarification:", text)
        current = next(event["text"] for event in events if event["type"] == "ai_input")
        self.assertNotIn("How does binary search work?", current)
        self.assertIn("So you mean the input needs to be sorted?", current)
        self.stop()

    def test_unknown_mixed_speech_is_manual_only(self):
        self.launch("unknown")
        self.speech("What is the difference between a type and an interface?")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.command("question Type vs interface in TypeScript?")
        self.until("answer_completed")
        self.stop()

    def test_flow_reports_actual_gate_and_composed_ai_input(self):
        self.launch("interviewee")
        self.speech("Explain binary search")
        self.until("transcript_updated")
        self.until("speech_status")  # final received, before debounce
        blocked = self.until("speech_status")[-1]
        self.assertIn("Not sent", blocked["text"])
        self.assertIn("Solo practice", blocked["text"])
        self.command("speaker practice")
        self.speech("Explain merge sort")
        events = self.until("answer_completed")
        sent = next(event["text"] for event in events if event["type"] == "ai_input")
        self.assertIn("Explain merge sort", sent)
        self.assertTrue(any(event["type"] == "question_detected" for event in events))
        self.stop()

    def test_combined_mode_answers_new_screen_without_speech_and_deduplicates(self):
        tree_file = self.root / "screen.json"

        def screen(question):
            tree_file.write_text(json.dumps({"schema": 1, "source": "atspi", "nodes": [
                {"id": 1, "parent": None, "role": "paragraph", "text": question, "focused": True}
            ]}))

        screen("Explain the time complexity of binary search on a sorted array.")
        helper = (self.root / "target/native-helpers/socket_stream_a11y"
                  if sys.platform == "darwin" else self.root / "scripts/linux-accessibility.py")
        helper.write_text(f"#!{sys.executable}\nimport pathlib\nprint(pathlib.Path({str(tree_file)!r}).read_text())\n")
        helper.chmod(0o700)
        self.launch("interviewee", read_screen=True)
        first = self.until("answer_completed")
        self.assertTrue(any(e["type"] == "question_detected" and e["source"] == "accessibility" for e in first))
        # Two interval reads must not dispatch the identical screen again.
        for _ in range(2):
            events = self.until("screen_read")
            self.assertFalse(any(e["type"] == "question_detected" for e in events))
        screen("Explain why merge sort has logarithmic recursion depth and linear merging.")
        second = self.until("answer_completed")
        sent = next(e["text"] for e in second if e["type"] == "ai_input")
        self.assertIn("merge sort", sent)
        self.assertNotIn("binary search", sent)
        self.stop()

    def _assert_next_closes_previous_http_stream(self, screen_only):
        disconnected = threading.Event()
        release = threading.Event()
        count = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b'{"data":[{"id":"fixture"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                count.append(True)
                first = len(count) == 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    text = "First partial" if first else "Next answer"
                    frame = {"choices": [{"delta": {"content": text}}]}
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                    if first:
                        while not release.wait(0.03):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    else:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    if first:
                        disconnected.set()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server():
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_server)
        if screen_only:
            tree = {"schema": 1, "source": "atspi", "nodes": [
                {"id": 1, "parent": None, "role": "paragraph", "text": "Explain the first screen question clearly.", "focused": True}
            ]}
            helper = (self.root / "target/native-helpers/socket_stream_a11y"
                      if sys.platform == "darwin" else self.root / "scripts/linux-accessibility.py")
            helper.write_text(f"#!{sys.executable}\nprint({json.dumps(tree)!r})\n")
            helper.chmod(0o700)
        self.launch("practice", provider="openai-compatible", screen_only=screen_only, extra_env={
            "SOCKET_STREAM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "SOCKET_STREAM_MODEL": "fixture", "SOCKET_STREAM_API_KEY": "",
        })
        if not screen_only:
            self.command("question First question")
        first = self.until("answer_delta")[-1]
        self.command("question Next queued question")
        self.command("next-question")
        events = self.until("answer_completed")
        self.assertTrue(any(e["type"] == "answer_cancelled" and e["request_id"] == first["request_id"] for e in events))
        next_id = next(e["request_id"] for e in events if e["type"] == "question_detected")
        self.assertNotEqual(next_id, first["request_id"])
        self.assertTrue(all(e["request_id"] == next_id for e in events if e["type"] == "answer_delta"))
        self.assertTrue(disconnected.wait(3), "Next must close the old HTTP stream, not just hide its output")
        self.assertIsNone(self.proc.poll(), "capture session must remain running")
        self.stop()

    def test_next_question_closes_audio_answer_stream_and_starts_queued_question(self):
        self._assert_next_closes_previous_http_stream(False)

    def test_next_question_closes_screen_answer_stream_and_starts_queued_question(self):
        self._assert_next_closes_previous_http_stream(True)

    def test_interviewer_questions_answered_statements_suppressed_and_role_switch_safe(self):
        self.launch("interviewer")
        self.speech("Type vs interface in TypeScript?")
        self.until("answer_completed")
        self.command("speaker interviewee")
        self.speech("How do hash maps work? I am repeating a question while answering.")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.command("speaker interviewer")
        self.speech("Thanks, that sounds good.")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.stop()


if __name__ == "__main__":
    unittest.main()
