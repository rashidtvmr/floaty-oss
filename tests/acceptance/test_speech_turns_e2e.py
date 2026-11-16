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

    def launch(self, role):
        env = dict(os.environ, SOCKET_STREAM_HOME=str(self.root),
                   SOCKET_STREAM_CONFIG=str(self.root / "missing-config.json"),
                   PATH=str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        self.proc = subprocess.Popen(
            [str(self.binary), "audio-live", "--authorized", "--source", "microphone",
             "--speaker-role", role, "--provider", "mock"],
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
        self.assertIn("Previous question: How does binary search work?", text)
        self.assertIn("Latest explicit clarification:", text)
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
