"""Synthetic PCM -> real bundled Moonshine -> CLI -> streaming mock LLM.

No microphone or system playback is opened. An isolated app root supplies a
synthetic capture executable; the production speech worker/model are unchanged.
Run after cargo build and scripts/build-moonshine.py (macOS with `say`).
"""
import json
import os
import fcntl
import queue
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "target/debug/socket_stream"
SPEECH = ROOT / "target/native-helpers/moonshine"


@unittest.skipUnless(sys.platform == "darwin" and CLI.is_file() and SPEECH.is_dir(),
                     "requires macOS say, built CLI and bundled Moonshine")
class MoonshineE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moonshine-e2e-", dir=ROOT / "target")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        helpers = self.root / "target/native-helpers"
        helpers.mkdir(parents=True)
        (helpers / "moonshine").symlink_to(SPEECH, target_is_directory=True)
        # project_root validates the root layout.
        (self.root / "scripts").mkdir()
        (self.root / "Cargo.toml").write_text("[workspace]\n")
        self.env = dict(os.environ, SOCKET_STREAM_HOME=str(self.root))
        self.marker = self.root / "capture.pid"
        self.wav = self.root / "synthetic.wav"
        subprocess.run(["/usr/bin/say", "-o", str(self.wav), "--data-format=LEI16@16000",
                        "Explain how binary search works."], check=True, capture_output=True)
        with wave.open(str(self.wav)) as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (16000, 1, 2))
        fake = helpers / "socket_stream_audio_stream"
        fake.write_text(f'''#!{sys.executable}
import os, sys, time, wave
from pathlib import Path
assert sys.argv[1:] == ["--authorized", "--source", "microphone"]
Path({str(self.marker)!r}).write_text(str(os.getpid()))
with wave.open({str(self.wav)!r}) as wav:
    while data := wav.readframes(1600):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        time.sleep(0.1)
while True:
    sys.stdout.buffer.write(b"\\0" * 3200)
    sys.stdout.buffer.flush()
    time.sleep(0.1)
''')
        fake.chmod(0o700)

    def launch(self, authorized=True):
        args = [str(CLI), "audio-live", "--stt", "moonshine", "--source", "microphone", "--provider", "mock"]
        if authorized:
            args.append("--authorized")
        self.proc = subprocess.Popen(args, env=self.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process)
        self.events = queue.Queue()

        def read():
            for line in self.proc.stdout:
                self.events.put(json.loads(line))
            self.events.put(None)
        threading.Thread(target=read, daemon=True).start()

    def cleanup_process(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()

    def until(self, kind):
        seen = []
        deadline = time.monotonic() + 45
        while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "overall event deadline exceeded")
            event = self.events.get(timeout=remaining)
            self.assertIsNotNone(event, "CLI exited before expected event")
            self.assertNotEqual(event["type"], "error", event.get("message"))
            seen.append(event)
            self.assertLessEqual(len(seen), 2048, "event retention limit exceeded")
            if event["type"] == kind:
                return seen

    def send(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def test_unauthorized_never_opens_audio(self):
        self.launch(False)
        self.assertNotEqual(self.proc.wait(timeout=5), 0)
        self.assertFalse(self.marker.exists())
        event = self.events.get(timeout=5)
        self.assertEqual(event["type"], "error")
        self.assertIn("authorized", event["message"])

    def test_stop_during_initialization_never_opens_audio(self):
        self.launch()
        self.until("session_started")
        self.send("stop")
        self.until("session_stopped")
        self.assertEqual(self.proc.wait(timeout=5), 0)
        self.assertFalse(self.marker.exists())

    def test_real_stt_answer_pause_resume_and_child_reaping(self):
        self.launch()
        seen = self.until("answer_completed")
        heard = [e["text"] for e in seen if e["type"] == "transcript_updated"]
        self.assertGreaterEqual(len(heard), 2, "expected partial and final transcript updates")
        self.assertIn("binary search", heard[-1].lower())
        self.assertTrue(any(e["type"] == "answer_delta" for e in seen))
        children = subprocess.check_output(["pgrep", "-P", str(self.proc.pid)], text=True).split()
        self.assertGreaterEqual(len(children), 2)
        first_capture = self.marker.read_text()
        self.send("pause")
        self.until("session_paused")
        for pid in children:
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid), 0)
        self.send("resume")
        self.until("session_resumed")
        self.until("answer_completed")
        self.assertNotEqual(first_capture, self.marker.read_text())
        last_capture = int(self.marker.read_text())
        self.send("stop")
        self.until("session_stopped")
        self.assertEqual(self.proc.wait(timeout=5), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(last_capture, 0)

    def test_control_owner_eof_stops_active_capture(self):
        self.launch()
        self.until("transcript_updated")
        children = subprocess.check_output(["pgrep", "-P", str(self.proc.pid)], text=True).split()
        self.assertGreaterEqual(len(children), 2)
        self.proc.stdin.close()
        self.until("session_stopped")
        self.assertEqual(self.proc.wait(timeout=5), 0)
        for pid in children:
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid), 0)

    def test_full_output_pipe_fails_closed_before_capture(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
        fcntl.fcntl(write_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        fcntl.fcntl(write_fd, fcntl.F_SETFL, flags)
        process = subprocess.Popen([str(CLI), "audio-live", "--authorized", "--provider", "mock"],
                                   env=self.env, stdin=subprocess.PIPE, stdout=write_fd,
                                   stderr=subprocess.DEVNULL)
        try:
            process.stdin.write(b"stop\n")
            process.stdin.flush()
            self.assertNotEqual(process.wait(timeout=3), 0)
            self.assertFalse(self.marker.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()

    def test_shared_terminal_keeps_blocking_input_and_accepts_stop(self):
        master, slave = os.openpty()
        flags = fcntl.fcntl(slave, fcntl.F_GETFL)
        process = subprocess.Popen([str(CLI), "audio-live", "--authorized", "--provider", "mock"],
                                   env=self.env, stdin=slave, stdout=slave,
                                   stderr=subprocess.DEVNULL)
        try:
            received = b""
            deadline = time.monotonic() + 10
            while b'"session_started"' not in received:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0)
                self.assertTrue(select.select([master], [], [], remaining)[0])
                received += os.read(master, 16384)
                self.assertLess(len(received), 131072)
            # Darwin may add internal terminal flags when a child first uses
            # the PTY. The regression concerns O_NONBLOCK on shared stdin.
            self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL) & os.O_NONBLOCK,
                             flags & os.O_NONBLOCK)
            self.assertIsNone(process.poll(), "idle terminal input is not EOF")
            os.write(master, b"stop\n")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL) & os.O_NONBLOCK,
                             flags & os.O_NONBLOCK)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
