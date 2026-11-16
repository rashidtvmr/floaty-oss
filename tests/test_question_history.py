"""Pinned Q&A stays coherent across streaming, cancellation and new sessions."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from live_state import HISTORY_LIMIT, LiveState


class QuestionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.state = LiveState()
        self.sequence = 0
        self.send("session_started")

    def send(self, kind, rid=None, **fields):
        self.sequence += 1
        event = dict(schema=1, type=kind, session_id="audio", sequence=self.sequence,
                     source="microphone", **fields)
        if rid is not None:
            event["request_id"] = rid
        return self.state.apply(event)

    def question(self, rid, answer=""):
        self.send("question_detected", rid, text=f"Question {rid}")
        if answer:
            self.send("answer_delta", rid, text=answer)

    def test_pin_streams_its_own_answer_and_holds_when_next_question_arrives(self):
        self.question("one", "First ")
        self.assertTrue(self.state.pin())
        self.send("answer_delta", "one", text="answer")
        self.send("answer_completed", "one")
        self.question("two", "Second answer")
        self.send("transcript_updated", text="More words while the model responds")
        view = self.state.display_snapshot()
        self.assertEqual((view["question"], view["answer"]), ("Question one", "First answer"))
        self.assertEqual(view["newer_count"], 1)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["session_status"], "thinking")
        self.assertTrue(view["pinned"])
        self.assertEqual(self.state.snapshot()["answer"], "Second answer")
        self.assertTrue(self.state.follow_latest())
        self.assertEqual(self.state.display_snapshot()["answer"], "Second answer")

    def test_superseded_pinned_request_cannot_receive_late_answer_or_error(self):
        self.question("one", "Partial")
        self.state.pin()
        self.question("two", "Current")
        self.assertFalse(self.send("answer_delta", "one", text="STALE"))
        self.assertFalse(self.send("answer_completed", "one"))
        self.assertFalse(self.send("error", "one", message="Old error"))
        self.assertEqual(self.state.display_snapshot()["answer"], "Partial")
        self.assertEqual(self.state.display_snapshot()["status"], "interrupted")
        self.assertEqual(self.state.snapshot()["error"], "")
        self.assertFalse(self.send("question_detected", "one", text="ID reused"))
        self.assertEqual(self.state.snapshot()["request_id"], "two")

    def test_history_is_bounded_but_pinned_pair_survives_eviction(self):
        self.question("pinned", "Retained")
        self.state.pin()
        for index in range(HISTORY_LIMIT * 4):
            self.question(str(index), "A" * 64001)
        history = self.state.history()
        self.assertEqual(len(history), HISTORY_LIMIT)
        self.assertEqual(history[0]["request_id"], "pinned")
        self.assertEqual(history[-1]["request_id"], str(HISTORY_LIMIT * 4 - 1))
        self.assertTrue(all(len(item["answer"]) <= 64000 for item in history))
        self.assertFalse(self.state.pin("evicted"))
        history[0]["answer"] = "external mutation"
        self.assertEqual(self.state.display_snapshot()["answer"], "Retained")

    def test_stop_closes_event_gate_immediately_and_preserves_pinned_answer(self):
        self.question("one", "Answer")
        self.send("answer_completed", "one")
        self.state.pin()
        self.question("two", "In progress")
        self.assertTrue(self.state.stop())
        self.assertFalse(self.send("answer_delta", "two", text="queued callback"))
        self.assertFalse(self.send("transcript_updated", text="post stop"))
        self.assertEqual(self.state.display_snapshot()["answer"], "Answer")
        self.assertEqual(self.state.display_snapshot()["session_status"], "stopped")
        self.assertFalse(self.state.stop())

    def test_pause_and_resume_reject_old_tokens_without_unpinning(self):
        self.question("one", "Partial")
        self.state.pin()
        self.send("session_paused")
        self.assertFalse(self.send("answer_delta", "one", text="paused"))
        self.send("session_resumed")
        self.assertFalse(self.send("answer_started", "one"))
        self.assertFalse(self.send("answer_delta", "one", text="late after resume"))
        self.assertFalse(self.send("answer_completed", "one"))
        self.assertFalse(self.send("error", "one", message="late after resume"))
        self.question("two", "Fresh")
        self.assertEqual(self.state.display_snapshot()["answer"], "Partial")
        self.assertEqual(self.state.display_snapshot()["status"], "interrupted")

    def test_stop_before_start_rejects_queued_startup_and_requires_new_state(self):
        state = LiveState()
        self.assertTrue(state.stop())
        for index, kind in enumerate(("session_started", "question_detected", "error"), 1):
            self.assertFalse(state.apply(dict(schema=1, type=kind, session_id="late", sequence=index,
                                              request_id="late", text="Late question", message="Late error")))
        self.assertEqual(state.snapshot()["status"], "stopped")
        self.assertEqual(state.history(), [])
        fresh = LiveState()
        self.assertTrue(fresh.apply(dict(schema=1, type="session_started", session_id="new", sequence=1)))

    def test_completion_and_error_cannot_be_reopened_by_late_tokens(self):
        for ending in ("answer_completed", "error"):
            with self.subTest(ending=ending):
                self.question(ending, "Retained answer")
                self.send(ending, ending, message="Request failed")
                before = self.state.display_snapshot()
                for kind in ("answer_started", "answer_delta", "answer_completed", "error"):
                    self.assertFalse(self.send(kind, ending, text="LATE", message="LATE"))
                self.assertEqual(self.state.display_snapshot(), before)

    def test_unpinned_latest_pair_retains_answer_status_after_stop(self):
        self.question("done", "Complete answer")
        self.send("answer_completed", "done")
        self.state.stop()
        view = self.state.display_snapshot()
        self.assertFalse(view["pinned"])
        self.assertEqual(view["answer"], "Complete answer")
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["session_status"], "stopped")

    def test_new_session_resets_memory_and_pin(self):
        self.question("one", "Old session")
        self.state.pin()
        self.send("session_stopped")
        self.send("session_started")
        self.assertEqual(self.state.history(), [])
        self.assertFalse(self.state.display_snapshot()["pinned"])
        self.question("one", "New session")
        self.assertEqual(self.state.display_snapshot()["answer"], "New session")

    def test_navigation_pins_adjacent_pair_and_never_wraps(self):
        self.assertFalse(self.state.toggle_pin())
        self.question("one", "A1")
        self.question("two", "A2")
        self.assertTrue(self.state.step_question(-1))
        self.assertEqual(self.state.display_snapshot()["answer"], "A1")
        self.assertFalse(self.state.step_question(-1))
        self.assertTrue(self.state.step_question(1))
        self.assertEqual(self.state.display_snapshot()["answer"], "A2")
        self.assertFalse(self.state.step_question(1))
        self.assertTrue(self.state.toggle_pin())
        self.assertFalse(self.state.display_snapshot()["pinned"])


if __name__ == "__main__":
    unittest.main()
