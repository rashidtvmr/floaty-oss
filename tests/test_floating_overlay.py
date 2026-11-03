"""Tests for the floating overlay module (headless: no GTK required)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "context-desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import floating_overlay as overlay  # noqa: E402


def event(kind, sequence, **fields):
    base = {
        "schema": 1, "type": kind, "session_id": "s1", "sequence": sequence,
        "source": "screen", "timestamp": "2026-11-03T00:00:00Z",
    }
    base.update(fields)
    return base


class ImportWithoutGtk(unittest.TestCase):
    def test_module_does_not_import_gtk(self):
        self.assertNotIn("gi", sys.modules)
        self.assertTrue(hasattr(overlay, "FloatingOverlay"))
        self.assertTrue(callable(overlay.main))

    def test_no_gtk_means_main_reports_cleanly(self):
        # On a machine without PyGObject, main() must fail gracefully, not crash.
        if "gi" in sys.modules:
            self.skipTest("GTK is installed on this machine")
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = overlay.main(["--stdin"])
        self.assertEqual(code, 2)
        self.assertIn("GTK3 unavailable", stderr.getvalue())


class ArgumentParsing(unittest.TestCase):
    def test_defaults(self):
        args = overlay.parse_args([])
        self.assertFalse(args.stdin)
        self.assertIsNone(args.provider)
        self.assertTrue(args.stealth)
        self.assertFalse(args.compact)
        self.assertEqual(args.interval, 4)

    def test_no_stealth_flag(self):
        self.assertFalse(overlay.parse_args(["--no-stealth"]).stealth)

    def test_stdin_and_provider(self):
        args = overlay.parse_args(["--stdin", "--provider", "ollama", "--compact"])
        self.assertTrue(args.stdin)
        self.assertEqual(args.provider, "ollama")
        self.assertTrue(args.compact)


class ObserveCommand(unittest.TestCase):
    def test_base_command(self):
        command = overlay.observe_command("/bin/context-cli", "ollama")
        self.assertEqual(command[:2], ["/bin/context-cli", "observe-live"])
        self.assertIn("--authorized", command)
        self.assertIn("ollama", command)
        self.assertNotIn("--allow-ocr", command)
        self.assertNotIn("--portal", command)

    def test_optional_flags(self):
        command = overlay.observe_command("/bin/context-cli", "mock", 9, True, True)
        self.assertIn("--allow-ocr", command)
        self.assertIn("--portal", command)
        self.assertEqual(command[command.index("--interval") + 1], "9")

    def test_default_cli_path_is_a_string(self):
        self.assertIsInstance(overlay.default_cli_path(), str)


class OverlayModel(unittest.TestCase):
    def test_feed_maps_events_to_view_state(self):
        model = overlay.OverlayModel()
        self.assertTrue(model.feed(event("session_started", 1)))
        self.assertTrue(model.feed(event(
            "question_detected", 2, request_id="q1",
            text="Find two numbers that sum to a target",
        )))
        self.assertTrue(model.feed(event(
            "answer_delta", 3, request_id="q1", text="Use a hash map.",
        )))
        self.assertTrue(model.feed(event("answer_completed", 4, request_id="q1")))
        state = model.snapshot()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["question"], "Find two numbers that sum to a target")
        self.assertEqual(state["answer"], "Use a hash map.")
        self.assertEqual(model.status_line("ollama"), "Ready · screen · ollama")

    def test_stale_answer_is_ignored(self):
        model = overlay.OverlayModel()
        model.feed(event("session_started", 1))
        model.feed(event("question_detected", 2, request_id="q1", text="First question here"))
        model.feed(event("question_detected", 3, request_id="q2", text="Second question here"))
        model.feed(event("answer_delta", 4, request_id="q1", text="stale"))
        self.assertEqual(model.snapshot()["answer"], "")

    def test_repeated_question_keeps_the_answer(self):
        model = overlay.OverlayModel()
        model.feed(event("session_started", 1))
        model.feed(event("question_detected", 2, request_id="q1", text="A long enough question"))
        model.feed(event("answer_delta", 3, request_id="q1", text="answer"))
        model.feed(event("answer_completed", 4, request_id="q1"))
        self.assertFalse(model.feed(event(
            "question_detected", 5, request_id="q2", text="A long enough question",
        )))
        self.assertEqual(model.snapshot()["answer"], "answer")


if __name__ == "__main__":
    unittest.main()
