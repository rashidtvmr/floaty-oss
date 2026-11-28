"""Adversarial session regressions using synthetic devices and loopback HTTP.

These tests inspect complete provider requests, including acknowledged history.
They do not read the desktop, record audio, or contact an external AI provider.
"""
import json
import queue
import time
import unittest

import test_incremental_screen_e2e as screen_fixture
import test_speech_turns_e2e as speech_fixture


class SessionHardeningE2E(unittest.TestCase):
    def screen_session(self):
        fixture = screen_fixture.IncrementalScreenE2E()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.start("audio-live")
        return fixture

    def speech_session(self):
        fixture = speech_fixture.SpeechTurnsE2E()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        provider = screen_fixture.Provider()
        self.addCleanup(provider.close)
        # Explicitly override the endpoint/key; never inherit a real service.
        fixture.launch("practice", provider="openai-compatible", extra_env={
            "SOCKET_STREAM_BASE_URL": f"http://127.0.0.1:{provider.server.server_port}/v1",
            "SOCKET_STREAM_MODEL": "fixture-model", "SOCKET_STREAM_API_KEY": "",
        })
        return fixture, provider

    def until_error(self, fixture):
        deadline = time.monotonic() + 10
        while True:
            try:
                event = fixture.events.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                self.fail("the failed provider must report an error")
            self.assertIsNotNone(event, "the session must survive a failed provider")
            if event["type"] == "error":
                return event
            self.assertLess(time.monotonic(), deadline)

    def test_paused_manual_question_runs_on_resume_with_acknowledged_history(self):
        fixture = self.screen_session()
        first = fixture.provider.requests[0]["messages"][-1]
        question = "Please explain the previously captured screen after resume."
        fixture.send("pause")
        fixture.until("session_paused")
        fixture.send("question " + question)
        fixture.send("resume")
        fixture.until("session_resumed")
        fixture.until("answer_completed")
        messages = fixture.provider.requests[-1]["messages"]
        self.assertEqual(len(fixture.provider.requests), 2)
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1], first)
        self.assertIn(question, messages[-1]["content"])
        # Pause discarded the current capture. Historical context is retained,
        # and the queued request must not claim it was read again on Resume.
        update = json.loads(messages[-1]["content"].split(screen_fixture.MARKER, 1)[1])
        self.assertEqual(update["kind"], "unavailable")

    def test_invalid_capture_reports_error_and_recovers_without_losing_session(self):
        fixture = self.screen_session()
        invalid = json.loads(fixture.tree_file.read_text())
        invalid["nodes"][2]["id"] = invalid["nodes"][1]["id"]
        temporary = fixture.tree_file.with_suffix(".new")
        temporary.write_text(json.dumps(invalid))
        temporary.replace(fixture.tree_file)
        fixture.send("read-screen")
        error = self.until_error(fixture)
        self.assertIn("parent identities", json.dumps(error))
        self.assertIsNone(fixture.proc.poll())
        self.assertEqual(len(fixture.provider.requests), 1)
        fixture.write_tree("Explain the recovered question after the invalid capture.")
        fixture.read()
        self.assertEqual(len(fixture.provider.requests), 2)
        self.assertEqual(fixture.update()["kind"], "patch")
        self.assertEqual(fixture.update()["base_revision"], 1)
        self.assertEqual(len(fixture.provider.requests[-1]["messages"]), 4)

    def test_identical_spoken_question_can_retry_after_provider_failure(self):
        fixture, provider = self.speech_session()
        question = "Explain how binary search handles duplicate values."
        provider.fail_next = True
        fixture.speech(question)
        self.until_error(fixture)
        fixture.speech(question)
        fixture.until("answer_completed")
        self.assertEqual(len(provider.requests), 2)
        messages = provider.requests[-1]["messages"]
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[-1]["content"], question)
        self.assertIsNone(fixture.proc.poll())

    def test_failed_question_does_not_replace_clarification_context(self):
        fixture, provider = self.speech_session()
        accepted = "Explain binary search on sorted input."
        failed = "Describe FAILED_UNACKNOWLEDGED_TOPIC instead."
        clarification = "Could you clarify that example?"
        fixture.command("question " + accepted)
        fixture.until("answer_completed")
        provider.fail_next = True
        fixture.command("question " + failed)
        self.until_error(fixture)
        fixture.command("question " + clarification)
        fixture.until("answer_completed")
        messages = provider.requests[-1]["messages"]
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], accepted)
        self.assertEqual(messages[2]["content"], "Fixture answer 1.")
        self.assertIn(clarification, messages[-1]["content"])
        self.assertNotIn(failed, json.dumps(messages))
        self.assertNotIn(accepted, messages[-1]["content"])
        self.assertNotIn("Fixture answer 1.", messages[-1]["content"])

    def test_long_unicode_clarification_uses_history_without_terminating_session(self):
        fixture, provider = self.speech_session()
        accepted = "Explain the following synthetic sequence: " + "a" * 7_000
        clarification = "Could you clarify that sequence? " + "\U0001f600" * 7_900
        fixture.command("question " + accepted)
        fixture.until("answer_completed")
        fixture.speech(clarification)
        fixture.until("answer_completed")
        messages = provider.requests[-1]["messages"]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(messages[1]["content"], accepted)
        self.assertIn(clarification, messages[-1]["content"])
        self.assertNotIn(accepted, messages[-1]["content"])
        self.assertIsNone(fixture.proc.poll())


if __name__ == "__main__":
    unittest.main()
