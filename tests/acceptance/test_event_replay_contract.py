"""RED until CLI replays synthetic JSONL events without capture or inference.

Run separately: python3 -m unittest discover -s tests/acceptance -v
"""
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "target/debug/context-cli"
FIXTURE = ROOT / "tests/fixtures/live_practice/events.jsonl"


class ReplayContract(unittest.TestCase):
    def test_replay_outputs_valid_jsonl_without_os_capture(self):
        self.assertTrue(CLI.is_file(), "Build the Rust CLI first: cargo build -p context-cli")
        env = os.environ.copy()
        env["CONTEXT_LENS_BASE_URL"] = "http://127.0.0.1:9/v1"
        env["CONTEXT_LENS_API_KEY"] = "should-not-appear-in-output"
        process = subprocess.run(
            [str(CLI), "replay-events", "--file", str(FIXTURE)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(process.returncode, 0,
            "Implement replay-events --file FILE with no OS capture or provider calls: "
            + process.stderr[:500])
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 13)
        self.assertEqual(events[0]["type"], "session_started")
        self.assertEqual(events[-1]["type"], "session_stopped")
        self.assertTrue(all(event["schema"] == 1 for event in events))
        self.assertNotIn("should-not-appear-in-output", process.stdout + process.stderr)

    def test_live_provider_preflight_fails_before_capture_with_ui_event(self):
        self.assertTrue(CLI.is_file(), "Build the Rust CLI first: cargo build -p context-cli")
        env = os.environ.copy()
        env["CONTEXT_LENS_BASE_URL"] = "http://127.0.0.1:9/v1"
        env["CONTEXT_LENS_MODEL"] = ""
        env["CONTEXT_LENS_API_KEY"] = "must-not-be-logged"
        process = subprocess.run(
            [str(CLI), "observe-live", "--authorized", "--provider", "openai-compatible"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=12,
        )
        self.assertNotEqual(process.returncode, 0)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertIn("Select a model", events[0]["message"])
        self.assertNotIn("must-not-be-logged", process.stdout + process.stderr)
