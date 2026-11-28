"""Independent session lifecycle regressions through the real CLI and HTTP.

The speech worker and provider are synthetic local fixtures. No desktop,
microphone, saved credentials, or external model is used.
"""
import json
import queue
import time
import unittest

import test_incremental_screen_e2e as screen_fixture
import test_speech_turns_e2e as speech_fixture


class SessionLifecycleE2E(unittest.TestCase):
    def speech_session(self):
        fixture = speech_fixture.SpeechTurnsE2E()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        provider = screen_fixture.Provider()
        self.addCleanup(provider.close)
        fixture.launch("practice", provider="openai-compatible", extra_env={
            "SOCKET_STREAM_BASE_URL": f"http://127.0.0.1:{provider.server.server_port}/v1",
            "SOCKET_STREAM_MODEL": "fixture-model", "SOCKET_STREAM_API_KEY": "",
        })
        return fixture, provider

    def hold_next_answer(self, provider):
        provider.entered.clear()
        provider.release.clear()
        provider.hold_next = True

    def wait_for_validation_barrier(self, fixture):
        # Controls are processed in FIFO order. This deliberately invalid,
        # nonqueued question confirms the preceding speaker change was applied
        # before the held HTTP response is released, without timing sleeps.
        fixture.command("question ?")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                event = fixture.events.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                self.fail("control validation barrier was not processed")
            self.assertIsNotNone(event, "CLI exited before processing controls")
            self.assertNotEqual(event["type"], "answer_completed")
            if event["type"] == "error":
                self.assertIn("Type a longer question", event.get("message", ""))
                return
        self.fail("control validation barrier timed out")

    def test_successful_reply_preserves_speaker_role_changed_while_in_flight(self):
        fixture, provider = self.speech_session()
        self.hold_next_answer(provider)
        fixture.command("question Explain binary search on sorted input.")
        self.assertTrue(provider.entered.wait(5), "provider must receive the first request")
        fixture.command("speaker unknown")
        self.wait_for_validation_barrier(fixture)
        provider.release.set()
        fixture.until("answer_completed")
        fixture.speech("Explain why merge sort uses extra storage.")
        fixture.until("transcript_updated")
        fixture.assert_no_automatic_question()
        self.assertEqual(len(provider.requests), 1,
                         "an older successful turn must not restore its previous speaker role")
        self.assertIsNone(fixture.proc.poll())

    def assert_cancelled_topic_is_not_acknowledged(self, pause):
        fixture, provider = self.speech_session()
        accepted = "Explain binary search on sorted input."
        cancelled = "Describe CANCELLED_UNACKNOWLEDGED_TOPIC instead."
        clarification = "Could you clarify that example?"
        fixture.command("question " + accepted)
        fixture.until("answer_completed")
        self.hold_next_answer(provider)
        fixture.command("question " + cancelled)
        self.assertTrue(provider.entered.wait(5), "provider must receive the cancelled request")
        if pause:
            fixture.command("pause")
            fixture.until("session_paused")
        else:
            fixture.command("next-question")
            fixture.until("answer_cancelled")
        provider.release.set()
        if pause:
            fixture.command("resume")
            fixture.until("session_resumed")
        fixture.command("question " + clarification)
        fixture.until("answer_completed")
        self.assertEqual(len(provider.requests), 3)
        messages = provider.requests[-1]["messages"]
        self.assertEqual([message["role"] for message in messages],
                         ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], accepted)
        self.assertEqual(messages[2]["content"], "Fixture answer 1.")
        self.assertIn(clarification, messages[-1]["content"])
        self.assertNotIn(cancelled, json.dumps(messages))
        self.assertNotIn(accepted, messages[-1]["content"])
        self.assertNotIn("Fixture answer 1.", messages[-1]["content"])
        self.assertIsNone(fixture.proc.poll())

    def test_next_question_does_not_acknowledge_cancelled_topic(self):
        self.assert_cancelled_topic_is_not_acknowledged(pause=False)

    def test_pause_does_not_acknowledge_cancelled_topic(self):
        self.assert_cancelled_topic_is_not_acknowledged(pause=True)

    def test_cancelled_spoken_question_can_be_repeated_immediately(self):
        fixture, provider = self.speech_session()
        question = "Explain how binary search handles duplicate values."
        self.hold_next_answer(provider)
        fixture.speech(question)
        self.assertTrue(provider.entered.wait(5), "provider must receive the spoken request")
        fixture.command("next-question")
        fixture.until("answer_cancelled")
        provider.release.set()
        fixture.speech(question)
        fixture.until("answer_completed")
        self.assertEqual(len(provider.requests), 2)
        messages = provider.requests[-1]["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertEqual(messages[-1]["content"], question)
        self.assertIsNone(fixture.proc.poll())


if __name__ == "__main__":
    unittest.main()
