"""Pure state machine for the visible, consent-based live-practice panel.

This module does no capture or inference. JSONL replay tests use it without GTK.
"""
import re

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
HISTORY_LIMIT = 24
_TYPES = {
    "session_started", "question_detected", "answer_started", "answer_delta",
    "answer_completed", "api_request", "api_response", "transcript_updated",
    "session_paused", "session_resumed", "error", "session_stopped",
    "screen_read", "token_usage",
}


def clean(value, limit):
    return _CONTROL.sub("", _ANSI.sub("", value))[:limit] if isinstance(value, str) else ""


class LiveState:
    """Latest-request inference with an independently pinned presentation.

    ``snapshot`` remains the live-engine contract. ``display_snapshot`` and
    ``history`` are the bounded, in-memory reading view; pinning never changes
    which request may accept tokens or starts any capture/inference work.
    """

    def __init__(self):
        self._view = dict(
            status="idle", session_id="", request_id="", question="",
            question_display="", answer="", source="", error="",
            api_request="", api_response="", transcript="", screen="",
            token_usage="Cache usage not reported",
        )
        self._seq = -1
        self._history = []
        self._pinned_id = ""
        self._closed = False
        self._request_open = False

    def snapshot(self):
        return self._view.copy()

    def history(self):
        return [item.copy() for item in self._history]

    def display_snapshot(self):
        view = self.snapshot()
        view.update(session_status=view["status"], live_error=view["error"],
                    latest_request_id=view["request_id"], pinned=False, newer_count=0)
        selected = self._pinned_id or view["request_id"]
        for index, item in enumerate(self._history):
            if item["request_id"] == selected:
                view.update(item)
                view.update(pinned=bool(self._pinned_id), newer_count=len(self._history) - index - 1)
                break
        return view

    def pin(self, request_id=None):
        request_id = self._view["request_id"] if request_id is None else request_id
        if request_id == self._pinned_id or not any(
                item["request_id"] == request_id for item in self._history):
            return False
        self._pinned_id = request_id
        return True

    def follow_latest(self):
        changed = bool(self._pinned_id)
        self._pinned_id = ""
        return changed

    def toggle_pin(self):
        return self.follow_latest() if self._pinned_id else self.pin()

    def step_question(self, direction):
        """Review an adjacent pair and hold it until Follow latest is chosen."""
        if direction not in (-1, 1) or not self._history:
            return False
        current = self._pinned_id or self._view["request_id"]
        index = next((i for i, item in enumerate(self._history)
                      if item["request_id"] == current), len(self._history) - 1)
        target = max(0, min(len(self._history) - 1, index + direction))
        return target != index and self.pin(self._history[target]["request_id"])

    def stop(self):
        """Close the event gate immediately, including queued GUI callbacks."""
        if self._closed:
            return False
        self._closed = True
        self._request_open = False
        self._view["status"] = "stopped"
        self._remember("session_stopped")
        return True

    def apply(self, event):
        if self._closed:
            return False
        # A request ID is immutable for its entire session, including a pinned
        # pair. Reusing an older ID must not replace its retained answer.
        if (isinstance(event, dict) and event.get("type") == "question_detected"
                and any(item["request_id"] == event.get("request_id")
                        for item in self._history)):
            return False
        changed = self._apply(event)
        if changed:
            self._remember(event["type"])
        return changed

    def _remember(self, kind):
        if kind == "session_started":
            self._history.clear()
            self._pinned_id = ""
            return
        rid = self._view["request_id"]
        if not rid:
            return
        entry = next((item for item in self._history if item["request_id"] == rid), None)
        if entry is None:
            for item in self._history:
                if item["status"] in ("thinking", "paused"):
                    item["status"] = "interrupted"
            entry = {}
            self._history.append(entry)
        previous_status = entry.get("status", "thinking")
        for name in ("session_id", "request_id", "question", "question_display",
                     "answer", "source", "error", "token_usage"):
            entry[name] = self._view[name]
        status = self._view["status"]
        if status == "stopped":
            status = previous_status if previous_status in ("ready", "error") else "interrupted"
        elif status == "paused":
            status = previous_status if previous_status in ("ready", "error") else "paused"
        elif status == "observing":
            status = previous_status if previous_status != "paused" else "interrupted"
        entry["status"] = status
        while len(self._history) > HISTORY_LIMIT:
            index = next(i for i, item in enumerate(self._history)
                         if item["request_id"] not in (self._pinned_id, rid))
            del self._history[index]

    def _apply(self, event):
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
            self._request_open = False
            source = event.get("source")
            self._view.update(
                status="observing", session_id=sid, request_id="",
                question="", question_display="", answer="",
                source=source if source in ("microphone", "playback") else "", error="",
                api_request="", api_response="", transcript="", screen="",
                token_usage="Cache usage not reported",
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
            self._request_open = False
            self._view["status"] = "stopped"
            return True
        if kind == "session_paused":
            if self._view["status"] == "paused":
                return False
            self._view["status"] = "paused"
            self._request_open = False
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
            rid = event.get("request_id")
            if rid is not None and (rid != self._view["request_id"] or not self._request_open):
                return False
            self._request_open = False
            self._view["status"] = "error"
            self._view["error"] = clean(event.get("message"), 500)
            return True

        if kind == "transcript_updated":
            self._view["transcript"] = clean(event.get("text"), 24000)
            return True

        if kind == "screen_read":
            self._view["screen"] = clean(event.get("text"), 12000)
            return True

        rid = event.get("request_id")
        if not isinstance(rid, str) or not 0 < len(rid) <= 128:
            return False
        if kind == "question_detected":
            question = clean(event.get("text"), 12000).strip()
            if not question or rid == self._view["request_id"]:
                return False
            display = clean(event.get("display"), 12000).strip()
            source = event.get("source")
            if source not in ("screen", "accessibility", "ocr", "microphone", "playback", "file", "manual"):
                source = "screen"
            self._view.update(
                status="thinking", request_id=rid, question=question,
                question_display=display or question,
                answer="", source=source, error="",
                api_request="", api_response="",
                token_usage="Cache usage not reported",
            )
            self._request_open = True
            return True
        if rid != self._view["request_id"]:
            return False
        if kind == "token_usage":
            self._view["token_usage"] = clean(event.get("text"), 200)
            return True
        if kind in ("answer_started", "answer_delta", "answer_completed") and not self._request_open:
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
            self._request_open = False
            self._view["status"] = "ready"
            return True
        if kind == "api_request":
            url = clean(event.get("url"), 500)
            model = clean(event.get("model"), 200)
            prompt = clean(event.get("prompt"), 4000)
            self._view["api_request"] = f"POST {url}\nmodel: {model}\n\n{prompt}"
            return True
        if kind == "api_response":
            status = event.get("status")
            status = f"HTTP {status}" if isinstance(status, int) and not isinstance(status, bool) else "local"
            text = clean(event.get("text"), 4000)
            self._view["api_response"] = f"{status}\n\n{text}"
            return True
        return False
