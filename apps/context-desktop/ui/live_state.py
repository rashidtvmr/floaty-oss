"""Pure state machine for the visible, consent-based live-practice panel.

This module does no capture or inference. JSONL replay tests use it without GTK.
"""
import re

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TYPES = {
    "session_started", "question_detected", "answer_started", "answer_delta",
    "answer_completed", "session_paused", "session_resumed", "error", "session_stopped",
}


def clean(value, limit):
    return _CONTROL.sub("", _ANSI.sub("", value))[:limit] if isinstance(value, str) else ""


class LiveState:
    """Only events for the latest question can change its answer."""

    def __init__(self):
        self._view = dict(
            status="idle", session_id="", request_id="", question="",
            answer="", source="", error="",
        )
        self._seq = -1

    def snapshot(self):
        return self._view.copy()

    def apply(self, event):
        if not isinstance(event, dict) or event.get("schema") != 1 or event.get("type") not in _TYPES:
            return False
        sequence = event.get("sequence")
        sid = event.get("session_id")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= self._seq:
            return False
        if not isinstance(sid, str) or not 0 < len(sid) <= 128:
            return False
        kind = event["type"]
        if kind == "session_started":
            if self._view["status"] not in ("idle", "stopped"):
                return False
            self._seq = sequence
            self._view.update(
                status="observing", session_id=sid, request_id="",
                question="", answer="", source="", error="",
            )
            return True
        if kind == "error" and self._view["status"] == "idle":
            source = event.get("source")
            self._seq = sequence
            self._view.update(
                status="error", session_id=sid,
                source=source if source in ("system", "provider", "accessibility", "ocr") else "system",
                error=clean(event.get("message"), 500),
            )
            return True
        if sid != self._view["session_id"] or self._view["status"] in ("idle", "stopped"):
            return False
        self._seq = sequence
        if kind == "session_stopped":
            self._view["status"] = "stopped"
            return True
        if kind == "session_paused":
            if self._view["status"] == "paused":
                return False
            self._view["status"] = "paused"
            return True
        if kind == "session_resumed":
            if self._view["status"] != "paused":
                return False
            self._view["status"] = "observing"
            self._view["error"] = ""
            return True
        if self._view["status"] == "paused":
            return False
        if kind == "error":
            self._view["status"] = "error"
            self._view["error"] = clean(event.get("message"), 500)
            return True

        rid = event.get("request_id")
        if not isinstance(rid, str) or not 0 < len(rid) <= 128:
            return False
        if kind == "question_detected":
            question = clean(event.get("text"), 12000).strip()
            if not question or question == self._view["question"]:
                return False
            source = event.get("source")
            if source not in ("screen", "accessibility", "ocr", "microphone", "playback", "file"):
                source = "screen"
            self._view.update(
                status="thinking", request_id=rid, question=question,
                answer="", source=source, error="",
            )
            return True
        if rid != self._view["request_id"]:
            return False
        if kind == "answer_started":
            if self._view["status"] == "thinking":
                return False
            self._view["status"] = "thinking"
            return True
        if kind == "answer_delta":
            if self._view["status"] == "ready":
                return False
            room = 64000 - len(self._view["answer"])
            addition = clean(event.get("text"), max(room, 0))
            if not addition:
                return False
            self._view["answer"] += addition
            self._view["status"] = "thinking"
            return True
        if kind == "answer_completed":
            self._view["status"] = "ready"
            return True
        return False
