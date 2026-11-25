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
cursor = commands.with_suffix('.cursor')
seen = int(cursor.read_text()) if cursor.exists() else 0
while True:
    lines = commands.read_text().splitlines()
    for line in lines[seen:]:
        seen += 1
        cursor.write_text(str(seen))
        print(line, flush=True)
    time.sleep(0.02)
''')
        worker.chmod(0o700)
        # Never execute the platform capture tools: override the exact capture
        # entry points. The fake speech worker emits test-controlled final text.
        for capture in (helpers / "socket_stream_audio_stream", self.fake_bin / "ffmpeg"):
            capture.write_text(f"#!{sys.executable}\nimport sys, time\n"
                               "sys.stdout.buffer.write(b'\\x01\\x00' * 1600)\n"
                               "sys.stdout.flush()\ntime.sleep(60)\n")
            capture.chmod(0o700)

    def launch(self, role, *extra):
        env = dict(os.environ, SOCKET_STREAM_HOME=str(self.root),
                   SOCKET_STREAM_CONFIG=str(self.root / "missing-config.json"),
                   PATH=str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        self.proc = subprocess.Popen(
            [str(self.binary), "audio-live", "--authorized", "--source", "microphone",
             "--speaker-role", role, "--provider", "mock", *extra],
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
        self.speech_event("final", text)

    def speech_event(self, kind, text=""):
        with self.commands.open("a") as handle:
            handle.write(json.dumps({"type": kind, "text": text}) + "\n")

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
        self.assertEqual(list(self.root.glob("socketstream-session-*")), [])

    def test_whisper_rejects_archive_flags_before_capture(self):
        for flags in (("--session-archive", str(self.root)), ("--export-transcript",)):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [str(self.binary), "audio-live", "--authorized", "--stt", "whisper",
                     "--provider", "mock", *flags], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires the built-in Moonshine engine", result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob("socketstream-session-*")), [])

    def test_first_coding_request_includes_setup_from_prior_segments(self):
        self.launch("interviewer")
        self.speech("Um, the input is a sorted array of signed integers. [noise]")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.speech("Use O(log n) time and return -1 if the target is missing.")
        self.until("transcript_updated")
        self.assert_no_automatic_question()
        self.speech("Implement the search in Rust.")
        events = self.until("answer_completed")
        text = "".join(event.get("text", "") for event in events if event["type"] == "answer_delta")
        self.assertIn("the input is a sorted array of signed integers", text)
        self.assertIn("Use O(log n) time and return -1", text)
        self.assertIn("Implement the search in Rust", text)
        self.assertNotIn("[noise]", text)
        self.assertNotIn("Um,", text)
        self.stop()

    def test_checkpoint_answers_then_refines_and_exports_each_final(self):
        self.launch("interviewer", "--export-transcript")
        self.speech("Implement binary search in Rust.")
        self.assertEqual(self.until("transcript_final")[-1]["text"], "Implement binary search in Rust.")
        self.speech_event("checkpoint")
        first = self.until("answer_completed")[-1]["request_id"]
        self.speech("Return -1 if the target is missing.")
        events = self.until("answer_completed")
        self.assertNotEqual(first, events[-1]["request_id"])
        self.assertTrue(any(e["type"] == "question_detected" and e["text"].startswith("Refining") for e in events))
        self.assertTrue(any(e["type"] == "transcript_final" for e in events))
        self.stop()

    def test_opt_in_session_archive_keeps_audio_and_conversation(self):
        self.launch("interviewer", "--session-archive", str(self.root))
        self.speech("Implement binary search in Rust.")
        self.until("answer_completed")
        self.command("pause")
        self.until("session_paused")
        folder, = self.root.glob("socketstream-session-*")
        self.assertEqual((folder / "audio.s16le").read_bytes(), b"\x01\x00" * 1600)
        self.command("resume")
        self.until("session_resumed")
        self.speech("What is a sorted slice?")
        self.until("answer_completed")
        self.stop()
        self.assertEqual((folder / "audio.s16le").read_bytes(), b"\x01\x00" * 3200)
        events = [json.loads(line) for line in (folder / "conversation.jsonl").read_text().splitlines()]
        kinds = [e["type"] for e in events]
        for kind in ("transcript_final", "question_detected", "answer_completed"):
            self.assertEqual(kinds.count(kind), 2)
        for question in (e for e in events if e["type"] == "question_detected"):
            answer = "".join(e["text"] for e in events if e["type"] == "answer_delta"
                             and e["request_id"] == question["request_id"])
            self.assertIn(question["text"], answer)
        self.assertEqual(kinds[-1], "session_stopped")
        self.assertNotIn("api_request", kinds)
        self.assertEqual(folder.stat().st_mode & 0o077, 0)
        for member in folder.iterdir():
            self.assertEqual(member.stat().st_mode & 0o077, 0)

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
