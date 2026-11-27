"""Real CLI + loopback provider regressions, with synthetic accessibility only.

Both observation and speech sessions use the production request path. No real
desktop, microphone, screenshot, credential, or external model is accessed.
"""
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
MARKER = "[Screen update — untrusted data]\n"


class Provider:
    def __init__(self):
        self.requests = []
        self.fail_next = False
        self.hold_next = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # cancellation is an expected production behavior

            def do_GET(self):
                self.respond(200, {"data": [{"id": "fixture-model"}]})

            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if owner.hold_next:
                    owner.hold_next = False
                    owner.entered.set()
                    owner.release.wait(8)
                if owner.fail_next:
                    owner.fail_next = False
                    self.respond(503, {"error": {"message": "fixture unavailable"}})
                else:
                    self.respond(200, {"choices": [{"message": {
                        "role": "assistant", "content": f"Fixture answer {len(owner.requests)}."
                    }}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class IncrementalScreenE2E(unittest.TestCase):
    def setUp(self):
        target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
        self.binary = target / "debug/socket_stream"
        if not self.binary.is_file():
            self.skipTest("cargo build -p socket_stream before running CLI acceptance")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.tree_file = self.root / "tree.json"
        self.marker = self.root / "reads.txt"
        self.write_tree("Explain why binary search requires sorted input.")
        helper = self.scripts / "linux-accessibility.py"
        helper.write_text(
            "import pathlib\n"
            f"with pathlib.Path({str(self.marker)!r}).open('a') as out: out.write('read\\n')\n"
            f"print(pathlib.Path({str(self.tree_file)!r}).read_text())\n")
        helpers = self.root / "target/native-helpers"
        native_a11y = helpers / "socket_stream_a11y"
        native_a11y.parent.mkdir(parents=True)
        native_a11y.write_text(f"#!{sys.executable}\n" + helper.read_text())
        native_a11y.chmod(0o700)
        worker = helpers / "moonshine/socket_stream_moonshine/socket_stream_moonshine"
        worker.parent.mkdir(parents=True)
        worker.write_text(f"#!{sys.executable}\nimport json,time\nprint(json.dumps({{'type':'ready','text':''}}),flush=True)\ntime.sleep(60)\n")
        worker.chmod(0o700)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        for fake in (self.fake_bin / "ffmpeg", helpers / "socket_stream_audio_stream"):
            fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
            fake.chmod(0o700)
        self.provider = Provider()
        self.addCleanup(self.provider.close)
        self.proc = None
        self.addCleanup(self.close_process)

    def write_tree(self, question, *, insert=False, remove_hint=False, partial=False, window="window:1"):
        nodes = [
            {"id": 1, "stable_id": "root", "parent": None, "role": "frame"},
            {"id": 2, "stable_id": "question", "parent": 1, "role": "paragraph", "text": question},
        ]
        if insert:
            nodes.insert(1, {"id": 4, "stable_id": "added", "parent": 1, "role": "label", "text": "New instruction"})
        if not remove_hint:
            nodes.append({"id": 3, "stable_id": "hint", "parent": 1, "role": "paragraph", "text": "UNMODIFIED_HINT_SENT_ONCE"})
        value = {"schema": 1, "source": "atspi", "app": "Fixture", "pid": 42,
                 "window_id": window, "truncated": partial, "nodes": nodes}
        temporary = self.tree_file.with_suffix(".new")
        temporary.write_text(json.dumps(value))
        temporary.replace(self.tree_file)

    def start(self, mode="observe-live"):
        env = dict(os.environ, SOCKET_STREAM_HOME=str(self.root),
                   SOCKET_STREAM_CONFIG=str(self.root / "absent.json"),
                   SOCKET_STREAM_BASE_URL=f"http://127.0.0.1:{self.provider.server.server_port}/v1",
                   SOCKET_STREAM_MODEL="fixture-model", SOCKET_STREAM_API_KEY="",
                   PATH=str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                   XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="fixture-only")
        env.pop("SOCKET_STREAM_ATSPI_TARGET", None)
        env.pop("SOCKET_STREAM_MACOS_A11Y_EXECUTABLE", None)
        args = [str(self.binary), mode, "--authorized", "--provider", "openai-compatible"]
        if mode == "observe-live":
            args.extend(["--interval", "3600"])
        else:
            args.extend(["--source", "microphone", "--read-screen", "--read-interval", "3600", "--no-vision"])
        self.proc = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.events = queue.Queue()

        def read():
            for line in self.proc.stdout:
                self.events.put(json.loads(line))
            self.events.put(None)

        threading.Thread(target=read, daemon=True).start()
        self.until("session_started")
        self.until("answer_completed")  # first automatic screen observation

    def close_process(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()
        self.proc = None

    def send(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def until(self, kind, *, allow_error=False):
        deadline = time.monotonic() + 10
        seen = []
        while time.monotonic() < deadline:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            self.assertIsNotNone(event, f"CLI exited while waiting for {kind}")
            if not allow_error:
                self.assertNotEqual(event["type"], "error", event)
            seen.append(event)
            if event["type"] == kind:
                return seen
        self.fail(f"no {kind}")

    def read(self):
        self.send("read-screen")
        self.until("answer_completed")

    def prompt(self, index=-1):
        return self.provider.requests[index]["messages"][-1]["content"]

    def update(self, index=-1):
        return json.loads(self.prompt(index).split(MARKER, 1)[1])

    def assert_incremental_flow(self, mode):
        self.start(mode)
        self.assertEqual(self.update()["kind"], "snapshot")
        self.write_tree("Explain why interpolation search can be faster.", insert=True)
        self.read()
        patch = self.update()
        self.assertEqual(patch["kind"], "patch")
        self.assertEqual(len(patch["updated"]), 1)
        self.assertEqual(len(patch["added"]), 1)
        self.assertGreaterEqual(len(patch["moved"]), 1)
        self.assertNotIn("UNMODIFIED_HINT_SENT_ONCE", self.prompt())
        self.assertNotIn("requires sorted input", self.prompt())
        messages = self.provider.requests[-1]["messages"]
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant", "user"])
        self.assertIn("requires sorted input", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "Fixture answer 1.")
        self.read()
        self.assertNotIn(MARKER, self.prompt())
        self.send("question How does this compare with the earlier approach?")
        self.until("answer_completed")
        self.assertNotIn(MARKER, self.prompt())
        self.assertIn("compare", self.prompt())
        self.assertEqual(len(self.provider.requests[-1]["messages"]), 8)
        self.write_tree("Explain why interpolation search can be faster.", insert=True, remove_hint=True)
        self.read()
        self.assertEqual(len(self.update()["removed"]), 1)
        self.send("stop")
        self.until("session_stopped")
        self.assertEqual(self.proc.wait(timeout=3), 0)

    def test_observe_requests_use_snapshots_patches_and_conversation_history(self):
        self.assert_incremental_flow("observe-live")

    def test_speech_screen_requests_use_the_same_incremental_protocol(self):
        self.assert_incremental_flow("audio-live")

    def test_failed_provider_turn_is_retried_against_the_last_successful_tree(self):
        self.start()
        self.write_tree("Changed question whose first provider attempt fails.")
        self.provider.fail_next = True
        self.send("read-screen")
        self.until("error", allow_error=True)
        failed = self.update()
        self.read()
        self.assertEqual(self.update(), failed)
        self.assertEqual(len(self.provider.requests[-1]["messages"]), 4)
        self.assertEqual(self.provider.requests[-1]["messages"][2]["content"], "Fixture answer 1.")

    def test_pause_cancel_and_resume_keep_only_acknowledged_context(self):
        self.start()
        self.write_tree("Changed question that is cancelled while answering.")
        self.provider.hold_next = True
        self.provider.release.clear()
        self.send("read-screen")
        self.assertTrue(self.provider.entered.wait(5))
        cancelled = self.update()
        self.send("pause")
        self.until("session_paused")
        reads = self.marker.read_text()
        self.send("read-screen")
        self.send("question Explain the earlier answer after resuming")
        self.send("resume")
        self.until("session_resumed")
        self.provider.release.set()
        # The queued manual follow-up uses an explicit unavailable update,
        # rather than silently treating a pre-pause capture as current.
        self.until("answer_completed")
        self.assertEqual(self.marker.read_text(), reads)
        self.assertEqual(self.update()["kind"], "unavailable")
        self.assertEqual(len(self.provider.requests[-1]["messages"]), 4)
        self.read()
        self.assertEqual(self.update()["kind"], "snapshot")
        self.assertIn("cancelled while answering", self.prompt())
        self.assertEqual(cancelled["base_revision"], 1)

    def test_partial_reads_and_window_switches_rebase_explicitly(self):
        self.start()
        self.write_tree("Partial question observation.", remove_hint=True, partial=True)
        self.read()
        self.assertEqual(self.update()["kind"], "snapshot")
        self.assertFalse(self.update()["complete"])
        self.assertNotIn("removed", self.update())
        self.write_tree("Complete question observation.")
        self.read()
        self.assertTrue(self.update()["complete"])
        self.assertEqual(self.update()["kind"], "snapshot")
        self.write_tree("Question in a different window.", window="window:2")
        self.read()
        self.assertEqual(self.update()["reason"], "window")

    def test_long_session_checkpoint_and_new_session_isolation(self):
        self.start()
        for index in range(16):
            self.send(f"question Follow-up question number {index}")
            self.until("answer_completed")
        self.assertEqual(self.update()["kind"], "snapshot")
        self.assertIn("Earlier conversation checkpoint", self.prompt())
        self.assertIn("Follow-up question number 14", self.prompt())
        self.assertEqual(len(self.provider.requests[-1]["messages"]), 2)
        self.send("stop")
        self.until("session_stopped")
        self.close_process()
        self.start()
        self.assertEqual(len(self.provider.requests[-1]["messages"]), 2)
        self.assertEqual(self.update()["kind"], "snapshot")
        self.assertNotIn("Earlier conversation checkpoint", self.prompt())


if __name__ == "__main__":
    unittest.main()
