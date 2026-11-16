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
    def test_live_flow_tracks_heard_gating_actual_input_and_waiting(self):
        self.state.apply(ev("transcript_updated", 2, text="Explain\nbinary search"))
        self.state.apply(ev("speech_status", 3, text="Not sent · My answers mode"))
        self.assertIn("Heard: Explain binary search", self.state.flow_text())
        self.assertIn("nothing sent", self.state.flow_text())
        self.assertIn("Not sent", self.state.flow_text())
        self.state.apply(ev("question_detected", 4, "q1", text="Explain binary search"), now=10)
        self.state.apply(ev("ai_input", 5, "q1", text="Actual question plus screen context"))
        self.state.apply(ev("answer_started", 6, "q1"))
        self.state.tick(13)
        self.assertIn("AI input: Actual question plus screen context", self.state.flow_text())
        self.assertIn("Waiting for AI first token · 3s", self.state.flow_text())
        self.assertNotIn("\n", self.state.flow_text())
        self.assertNotIn("Actual question", self.state.diagnostic_text())
        self.state.apply(ev("answer_completed", 7, "q1"))
        self.assertFalse(self.state.apply(ev("ai_input", 8, "q1", text="late input")))

    def test_real_progress_and_provider_error_category(self):
        self.state.apply(ev("question_detected", 2, "q1", text="What is a closure?"), now=10)
        self.state.apply(ev("api_request", 3, "q1", url="https://example.test/v1", model="test"), now=11)
        self.assertTrue(self.state.tick(15))
        self.assertIn("first token", self.state.progress_text())
        self.assertIn("5s", self.state.progress_text())
        self.state.apply(ev("answer_delta", 4, "q1", text="A closure"), now=16)
        self.assertEqual(self.state.snapshot()["stage"], "streaming")
        self.state.apply(ev("api_response", 5, "q1", status=503))
        self.state.apply(ev("error", 6, "q1", source="provider", message="Provider rejected request"))
        self.assertEqual(self.state.snapshot()["error_kind"], "provider unavailable")
        self.assertIn("HTTP 503", self.state.diagnostic_text())
        self.assertNotIn("What is a closure", self.state.diagnostic_text())
        self.assertFalse(self.state.tick(100))
        self.state.stop()
        self.assertEqual(self.state.snapshot()["stage"], "stopped")

    def test_usage_is_request_scoped_preserved_in_history_and_not_a_guarantee(self):
        self.assertEqual(self.state.snapshot()["token_usage"], "Cache usage not reported")
        self.state.apply(ev("question_detected", 2, "q1", text="Type vs interface?"))
        self.state.apply(ev("token_usage", 3, "q1", text="Tokens · input 1500 · cached 1024 · output 80"))
        self.assertIn("cached 1024", self.state.snapshot()["token_usage"])
        self.state.apply(ev("answer_completed", 4, "q1"))
        self.state.pin("q1")
        self.state.apply(ev("question_detected", 5, "q2", text="What is a closure?"))
        self.assertEqual(self.state.snapshot()["token_usage"], "Cache usage not reported")
        self.assertIn("cached 1024", self.state.display_snapshot()["token_usage"])
        self.assertFalse(self.state.apply(ev("token_usage", 6, "q1", text="stale")))
        self.state.apply(ev("token_usage", 7, "q2", text="x" * 500))
        self.assertEqual(len(self.state.snapshot()["token_usage"]), 200)

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
            "answer", "source", "error", "transcript", "api_request", "api_response", "token_usage",
            "screen", "stage", "error_kind", "elapsed",
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

    def test_screen_read_updates_screen_view(self):
        self.state.apply(ev("screen_read", 2, source="accessibility",
                            status="ok", text="def binary_search(a, x): …"))
        self.assertEqual(self.state.snapshot()["screen"], "def binary_search(a, x): …")
        # Unavailable reads render their bounded reason text too.
        self.state.apply(ev("screen_read", 3, source="system",
                            status="unavailable", text="y" * 20000))
        self.assertEqual(len(self.state.snapshot()["screen"]), 12000)
        # A new session clears the previous screen context.
        self.state.apply(ev("session_stopped", 4))
        self.state.apply(ev("session_started", 5, sid="second"))
        self.assertEqual(self.state.snapshot()["screen"], "")

    def test_screen_read_is_ignored_after_stop(self):
        self.state.apply(ev("session_stopped", 2))
        self.assertFalse(self.state.apply(ev("screen_read", 3, source="ocr",
                                             status="ok", text="late read")))
        self.assertEqual(self.state.snapshot()["screen"], "")

    def test_manual_question_source_is_accepted(self):
        self.state.apply(ev("question_detected", 2, "m1", source="manual",
                            text="Why does this fail?"))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["source"], "manual")
        self.assertEqual(snapshot["status"], "thinking")
        self.assertEqual(snapshot["question"], "Why does this fail?")


if __name__ == "__main__":
    unittest.main()
