"""Real state-machine tests for live screen/audio practice updates (no GTK required)."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/socket_stream_desktop/ui/live_state.py"
spec = importlib.util.spec_from_file_location("socket_stream_live_state", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def ev(typ, seq, rid=None, sid="demo", **extra):
    data = {
        "schema": 1, "type": typ, "session_id": sid,
        "sequence": seq, "source": "screen", "timestamp": "2026-11-03T00:00:00Z",
    }
    if rid:
        data["request_id"] = rid
    data.update(extra)
    return data


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.state = mod.LiveState()
        self.assertTrue(self.state.apply(ev("session_started", 1)))

    def test_question_and_delta_and_completion(self):
        self.assertTrue(self.state.apply(ev("question_detected", 2, "q1", text="Two sum?")))
        self.assertEqual(self.state.snapshot()["status"], "thinking")
        self.state.apply(ev("answer_started", 3, "q1"))
        self.assertTrue(self.state.apply(ev("answer_delta", 4, "q1", text="Hash ")))
        self.assertTrue(self.state.apply(ev("answer_delta", 5, "q1", text="map.")))
        self.assertEqual(self.state.snapshot()["answer"], "Hash map.")
        self.state.apply(ev("answer_completed", 6, "q1"))
        self.assertEqual(self.state.snapshot()["status"], "ready")

    def test_duplicate_does_not_reset_response(self):
        self.state.apply(ev("question_detected", 2, "q1", text="Question one"))
        self.state.apply(ev("answer_delta", 3, "q1", text="Response one"))
        self.state.apply(ev("answer_completed", 4, "q1"))
        self.assertFalse(self.state.apply(ev("question_detected", 5, "q1", text="Question one")))
        self.assertEqual(self.state.snapshot()["answer"], "Response one")

    def test_latest_question_wins(self):
        self.state.apply(ev("question_detected", 2, "q1", text="First"))
        self.state.apply(ev("answer_delta", 3, "q1", text="Early"))
        self.state.apply(ev("question_detected", 4, "q2", text="Second", source="microphone"))
        self.assertEqual(self.state.snapshot()["answer"], "")
        self.assertFalse(self.state.apply(ev("answer_delta", 5, "q1", text="STALE")))
        self.state.apply(ev("answer_delta", 6, "q2", text="Current"))
        self.assertEqual(self.state.snapshot()["source"], "microphone")
        self.assertEqual(self.state.snapshot()["answer"], "Current")

    def test_stop_rejects_future_events_and_preserves_answer(self):
        self.state.apply(ev("question_detected", 2, "q1", text="Q"))
        self.state.apply(ev("answer_delta", 3, "q1", text="A"))
        self.state.apply(ev("session_stopped", 4))
        stopped = self.state.snapshot()
        self.assertFalse(self.state.apply(ev("answer_delta", 5, "q1", text="STILL GENERATING")))
        self.assertFalse(self.state.apply(ev("question_detected", 6, "q2", text="Q2")))
        self.assertEqual(self.state.snapshot(), stopped)

    def test_paused_ignores_answer_then_resumes(self):
        self.state.apply(ev("question_detected", 2, "q1", text="Q1"))
        self.assertTrue(self.state.apply(ev("session_paused", 3)))
        self.assertFalse(self.state.apply(ev("answer_delta", 4, "q1", text="PAUSED")))
        self.assertTrue(self.state.apply(ev("session_resumed", 5)))
        self.assertTrue(self.state.apply(ev("question_detected", 6, "q2", text="Q2")))
        self.assertEqual(self.state.snapshot()["status"], "thinking")

    def test_foreign_session_old_seq_invalid_schema(self):
        original = self.state.snapshot()
        self.assertFalse(self.state.apply(ev("question_detected", 2, "q1", sid="foreign", text="secret")))
        self.assertFalse(self.state.apply(ev("question_detected", 1, "q1", text="old")))
        event = ev("question_detected", 2, "q1", text="wrong")
        event["schema"] = 888
        self.assertFalse(self.state.apply(event))
        self.assertEqual(self.state.snapshot(), original)

    def test_bounds_input_and_never_displays_extra_keys(self):
        self.state.apply(ev(
            "question_detected", 2, "q1",
            text="X" * 90000 + "\x1b[31m",
            api_key="SECRET_API_KEY", raw_audio="SENSITIVE AUDIO",
        ))
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["question"]), 12000)
        self.assertNotIn("SECRET_API_KEY", json.dumps(snapshot))
        self.assertNotIn("SENSITIVE AUDIO", json.dumps(snapshot))
        self.assertEqual(set(snapshot), {
            "status", "session_id", "request_id", "question", "question_display",
            "answer", "source", "error", "transcript", "api_request", "api_response",
        })
        # Without a display field the panel falls back to the raw question.
        self.assertEqual(snapshot["question_display"], snapshot["question"])
        self.state.apply(ev("answer_delta", 3, "q1", text="A" * 100000))
        self.assertEqual(len(self.state.snapshot()["answer"]), 64000)

    def test_transcript_is_bounded_and_session_scoped_without_changing_answer(self):
        self.state.apply(ev("question_detected", 2, "q1", text="Explain search"))
        self.state.apply(ev("answer_delta", 3, "q1", text="Split the range"))
        self.assertTrue(self.state.apply(ev("transcript_updated", 4, text="\x1b[31mheard\x00")))
        self.assertEqual(self.state.snapshot()["transcript"], "heard")
        self.assertEqual(self.state.snapshot()["answer"], "Split the range")
        self.assertFalse(self.state.apply(ev("transcript_updated", 5, sid="other", text="foreign")))
        self.state.apply(ev("session_paused", 6))
        self.assertFalse(self.state.apply(ev("transcript_updated", 7, text="paused")))
        self.state.apply(ev("session_resumed", 8))
        self.state.apply(ev("transcript_updated", 9, text="x" * 30000))
        self.assertEqual(len(self.state.snapshot()["transcript"]), 24000)
        self.state.apply(ev("session_stopped", 10))
        self.assertFalse(self.state.apply(ev("transcript_updated", 11, text="stopped")))
        self.state.apply(ev("session_started", 12, sid="new"))
        self.assertEqual(self.state.snapshot()["transcript"], "")

    def test_identical_question_after_resume_accepts_new_answer(self):
        self.state.apply(ev("question_detected", 2, "spoken-1", text="Explain search"))
        self.state.apply(ev("answer_delta", 3, "spoken-1", text="Partial"))
        self.state.apply(ev("session_paused", 4))
        self.state.apply(ev("session_resumed", 5))
        self.assertTrue(self.state.apply(ev("question_detected", 6, "spoken-2", text="Explain search")))
        self.assertEqual(self.state.snapshot()["answer"], "")
        self.assertFalse(self.state.apply(ev("answer_delta", 7, "spoken-1", text="stale")))
        self.assertTrue(self.state.apply(ev("answer_delta", 8, "spoken-2", text="Fresh answer")))
        self.assertTrue(self.state.apply(ev("answer_completed", 9, "spoken-2")))
        self.assertEqual(self.state.snapshot()["answer"], "Fresh answer")

    def test_role_free_display_text_is_used_when_present(self):
        self.state.apply(ev(
            "question_detected", 2, "q1",
            text="window LeetCode\n  textarea Two Sum",
            display="LeetCode\nTwo Sum",
        ))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["question"], "window LeetCode\n  textarea Two Sum")
        self.assertEqual(snapshot["question_display"], "LeetCode\nTwo Sum")

    def test_error_and_session_restart(self):
        self.state.apply(ev("error", 2, message="Provider unavailable"))
        self.assertEqual(self.state.snapshot()["status"], "error")
        self.state.apply(ev("session_stopped", 3))
        self.assertTrue(self.state.apply(ev("session_started", 4, sid="second")))
        self.assertEqual(self.state.snapshot()["session_id"], "second")

    def test_provider_preflight_error_before_capture(self):
        state = mod.LiveState()
        self.assertTrue(state.apply(ev("error", 1, sid="startup", source="provider",
                                       message="Provider unavailable")))
        snapshot = state.snapshot()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["source"], "provider")
        self.assertEqual(snapshot["error"], "Provider unavailable")


if __name__ == "__main__":
    unittest.main()
