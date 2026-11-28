"""A delayed pre-pause screen read must not become current after Resume.

Exercises the real audio-live reader thread and provider request path with
synthetic accessibility/speech helpers and a loopback provider. No screen,
microphone, native accessibility bus or external model is accessed.
"""
import json
import sys
import time
import unittest

import test_incremental_screen_e2e as screen_fixture


class ScreenPauseGenerationE2E(unittest.TestCase):
    def setUp(self):
        # Reuse the fixture without inheriting and rerunning its test methods.
        self.fixture = screen_fixture.IncrementalScreenE2E()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_delayed_read_from_before_pause_is_discarded_after_resume(self):
        fixture = self.fixture
        fixture.start("audio-live")
        self.assertEqual(len(fixture.provider.requests), 1)
        entered = fixture.root / "blocked-screen-read.entered"
        release = fixture.root / "blocked-screen-read.release"
        self.addCleanup(release.touch)
        stale = "Explain why STALE_CAPTURE_BEFORE_PAUSE must never become current."
        fresh = "Explain why FRESH_CAPTURE_AFTER_RESUME is the current question."
        fixture.write_tree(stale)
        helper = fixture.scripts / "linux-accessibility.py"
        helper.write_text(
            "import pathlib,time\n"
            f"snapshot=pathlib.Path({str(fixture.tree_file)!r}).read_text()\n"
            f"entered=pathlib.Path({str(entered)!r})\n"
            f"release=pathlib.Path({str(release)!r})\n"
            "if not entered.exists():\n"
            "    entered.touch()\n"
            "    deadline=time.monotonic()+5\n"
            "    while not release.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
            "print(snapshot)\n"
        )
        native = fixture.root / "target/native-helpers/socket_stream_a11y"
        native.write_text(f"#!{sys.executable}\n" + helper.read_text())
        native.chmod(0o700)

        fixture.send("read-screen")
        deadline = time.monotonic() + 4
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(entered.exists(), "the old-generation helper must capture before Pause")
        fixture.send("pause")
        fixture.until("session_paused")
        fixture.write_tree(fresh)
        fixture.send("resume")
        fixture.until("session_resumed")
        release.touch()

        accepted = fixture.until("screen_read")[-1]
        self.assertIn("FRESH_CAPTURE_AFTER_RESUME", accepted["text"])
        self.assertNotIn("STALE_CAPTURE_BEFORE_PAUSE", json.dumps(accepted))
        fixture.until("answer_completed")
        self.assertEqual(len(fixture.provider.requests), 2)
        self.assertIn("FRESH_CAPTURE_AFTER_RESUME", fixture.prompt())
        self.assertNotIn("STALE_CAPTURE_BEFORE_PAUSE", json.dumps(fixture.provider.requests))
        self.assertEqual(fixture.update()["kind"], "patch")
        fixture.send("stop")
        fixture.until("session_stopped")
        self.assertEqual(fixture.proc.wait(timeout=3), 0)


if __name__ == "__main__":
    unittest.main()
