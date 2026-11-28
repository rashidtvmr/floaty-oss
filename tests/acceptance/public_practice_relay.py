#!/usr/bin/env python3
"""Opt-in ONE-request relay for the approved public Two Sum practice page.

The desktop receives a random loopback token, never the real API key. Its
caller proves ownership of the selected Chrome window on the real user bus
and supplies a fresh native tree. Only the actual production CLI messages are
forwarded. The controller uses the existing fixed-host TLS transport; the sole
request changes are an output cap, disabled storage and requested usage.

Use --https-proxy "$HTTPS_PROXY" in the SAME controller execution when that
execution requires its explicitly configured CONNECT proxy. No proxy is read
implicitly and no port is hardcoded. Keys use hidden TTY or an anonymous FD.
The original SSE remains private in the response file for independent answer
review. Structural answer checks are not a proof of algorithm correctness.
The relay buffers SSE and does not measure streaming latency.

  python3 public_practice_relay.py --relay-dir /owned/relay --key-fd FD
  python3 public_practice_relay.py --self-test
"""
import base64
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

import openai_live_smoke as smoke
from screen_model_relay import RelayDirectory, node_map, safe_code


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_URL = "https://leetcode.com/problems/two-sum/description/"
MODEL = smoke.MODEL
MAX_REQUEST_BYTES = 64_000
MAX_COMPLETION_TOKENS = 2048
MAX_ANSWER_BYTES = 24_000
require = smoke.require
RelayFailure = smoke.SmokeFailure
HEX = re.compile(r"[a-f0-9]{64}\Z")
SYSTEM = (ROOT / "crates/socket_stream_core/prompts/practice-system-v3.md").read_text(encoding="utf-8")


def compact(text):
    return re.sub(r"\s+", "", text).casefold()


def markers_present(text):
    value = compact(text)
    return all(marker in value for marker in (
        "twosum", "nums=[2,7,11,15]", "target=9", "indices",
        "youmaynotusethesameelementtwice"))


def semantic_node(node):
    """Fields bound to a separate native read; focus/layout may change."""
    return {key: node.get(key, default) for key, default in (
        ("id", None), ("parent", None), ("position", None), ("role", ""),
        ("name", ""), ("text", ""), ("image", False))}


def validate_scope(scope):
    require(isinstance(scope, dict) and set(scope) == {
        "page_url", "window_id", "window_title", "browser_pid",
        "user_bus_verified", "owned_profile_verified"}, "practice_scope_invalid")
    require(scope["page_url"] == PUBLIC_URL and scope["user_bus_verified"] is True
            and scope["owned_profile_verified"] is True, "practice_scope_unverified")
    require(type(scope["browser_pid"]) is int and 0 < scope["browser_pid"] < 2**32,
            "practice_browser_pid_invalid")
    title = scope["window_title"]
    require(isinstance(title, str) and len(title.encode()) <= 512
            and "two sum" in title.casefold() and "leetcode" in title.casefold(),
            "practice_title_mismatch")
    identity = scope["window_id"]
    require(isinstance(identity, str) and len(identity) <= 4096, "practice_window_id_invalid")
    decoded = smoke.strict_json(identity)
    require(isinstance(decoded, list) and len(decoded) == 2
            and isinstance(decoded[0], str) and re.fullmatch(r":[0-9]+\.[0-9]+", decoded[0])
            and isinstance(decoded[1], str) and re.fullmatch(r"/[A-Za-z0-9_/]*", decoded[1]),
            "practice_window_id_invalid")


def validate_request(request, scope):
    validate_scope(scope)
    require(isinstance(request, dict) and set(request) == {"model", "messages", "stream"}
            and request["model"] == MODEL and request["stream"] is True, "practice_request_invalid")
    require(len(json.dumps(request, ensure_ascii=False).encode()) <= MAX_REQUEST_BYTES,
            "practice_request_too_large")
    messages = request["messages"]
    require(isinstance(messages, list) and len(messages) == 2
            and all(isinstance(message, dict) and set(message) == {"role", "content"}
                    and message["role"] == role and isinstance(message["content"], str)
                    for message, role in zip(messages, ("system", "user"))), "practice_history_invalid")
    require(messages[0]["content"] == SYSTEM, "practice_system_mismatch")
    prompt = messages[1]["content"]
    require(prompt.startswith(smoke.PREFIX), "practice_screen_protocol_mismatch")
    raw = prompt[len(smoke.PREFIX):]
    snapshot = smoke.strict_json(raw)
    require(raw == json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            "practice_screen_encoding_mismatch")
    require(isinstance(snapshot, dict) and set(snapshot) == {
        "kind", "schema", "revision", "window", "complete", "reason", "nodes"}
            and snapshot["kind"] == "snapshot" and type(snapshot["schema"]) is int
            and snapshot["schema"] == 1 and type(snapshot["revision"]) is int
            and snapshot["revision"] == 1 and type(snapshot["complete"]) is bool
            and snapshot["reason"] == "initial", "practice_snapshot_invalid")
    window = snapshot["window"]
    require(isinstance(window, dict) and set(window) == {"source", "app", "pid", "id"}
            and window["source"] == "atspi" and window["id"] == scope["window_id"]
            and type(window["pid"]) is int and window["pid"] == scope["browser_pid"]
            and isinstance(window["app"], str) and len(window["app"].encode()) <= 512,
            "practice_window_mismatch")
    node_map(snapshot["nodes"])
    text = "\n".join(node.get(key, "") for node in snapshot["nodes"] for key in ("name", "text"))
    require(markers_present(text), "practice_question_markers_missing")
    return snapshot


def native_projection(tree, window):
    """Independent protected/chrome filtering and structural pruning.

    This runs only on the caller's already scoped native read. It never opens
    a bus or selects a window. Retain native keys for binding to model IDs.
    """
    require(isinstance(tree, dict) and type(tree.get("schema")) is int and tree.get("schema") == 1
            and type(tree.get("truncated")) is bool
            and {"source": tree.get("source"), "app": tree.get("app", ""),
                 "pid": tree.get("pid"), "id": tree.get("window_id")} == window,
            "practice_native_window_mismatch")
    raw = tree.get("nodes")
    require(isinstance(raw, list) and 0 < len(raw) <= 4096
            and len(json.dumps(tree, ensure_ascii=False).encode()) <= 1_048_576,
            "practice_native_tree_invalid")
    index, children = {}, {}
    for node in raw:
        require(isinstance(node, dict) and type(node.get("id")) is int and node["id"] not in index
                and (node.get("parent") is None or type(node["parent"]) is int)
                and isinstance(node.get("role"), str)
                and all(isinstance(node.get(key, ""), str) for key in ("name", "text"))
                and all(type(node.get(key, False)) is bool for key in ("focused", "image", "protected")),
                "practice_native_node_invalid")
        index[node["id"]] = node
        children.setdefault(node.get("parent"), []).append(node["id"])
    require(all(node.get("parent") is None or node["parent"] in index for node in raw),
            "practice_native_parent_missing")
    stack, seen, excluded, ordered = list(reversed(children.get(None, []))), set(), set(), []
    while stack:
        identity = stack.pop()
        require(identity not in seen, "practice_native_cycle")
        seen.add(identity)
        node = index[identity]
        role = node["role"].lower()
        chrome = re.sub(r"[ _-]", "", role) in {"toolbar", "menubar", "menu", "navigation", "banner", "statusbar"}
        if node.get("parent") in excluded or node.get("protected", False) or chrome or "password" in role or "secure" in role:
            excluded.add(identity)
        else:
            node = dict(node)
            if node["role"] in {"application", "window", "frame"}:
                node.update(name="", text="")
            ordered.append(node)
        stack.extend(reversed(children.get(identity, [])))
    require(len(seen) == len(raw), "practice_native_cycle")
    eligible, retained = {node["id"]: node for node in ordered}, set()
    for node in reversed(ordered):
        if (node["id"] in retained or node.get("parent") is None
                or node["role"].strip().lower() not in {"panel", "filler", "separator", "scroll pane", "unknown"}
                or node.get("name", "") or node.get("text", "") or node.get("focused", False) or node.get("image", False)):
            retained.add(node["id"])
            if node.get("parent") is not None:
                require(node["parent"] in eligible, "practice_native_parent_filtered")
                retained.add(node["parent"])
    projected, stable_keys, sibling_counts = [], set(), {}
    for node in ordered:
        if node["id"] not in retained:
            continue
        stable = node.get("stable_id")
        require(isinstance(stable, str) and 0 < len(stable.encode()) <= 1024
                and not any(ord(character) < 32 or 127 <= ord(character) < 160 for character in stable)
                and stable not in stable_keys, "practice_native_identity_invalid")
        stable_keys.add(stable)
        parent = eligible[node["parent"]].get("stable_id") if node.get("parent") is not None else None
        position = sibling_counts.get(parent, 0)
        sibling_counts[parent] = position + 1
        projected.append(semantic_node(dict(node, id=stable, parent=parent, position=position)))
    return projected


def bind_native_tree(tree, snapshot, scope):
    native = native_projection(tree, snapshot["window"])
    model = snapshot["nodes"]
    require(len(native) >= len(model) and (not snapshot["complete"] or len(native) == len(model)),
            "practice_native_projection_size_mismatch")
    require(not snapshot["complete"] or not tree["truncated"], "practice_native_completeness_mismatch")
    mapping = {item["id"]: node["id"] for item, node in zip(native, model)}
    for offset, (item, node) in enumerate(zip(native, model)):
        require(item["parent"] is None or item["parent"] in mapping, "practice_native_parent_mismatch")
        expected = dict(item, id=node["id"], parent=mapping.get(item["parent"]))
        actual = semantic_node(node)
        if not snapshot["complete"] and offset == len(model) - 1:
            # The production byte budget may shorten only the final emitted
            # node. A prefix can omit public data, never introduce other data.
            for key in ("name", "text"):
                require(expected[key].startswith(actual[key]), "practice_native_text_mismatch")
                expected[key] = actual[key]
        require(actual == expected, "practice_native_projection_mismatch")
    return {
        "scope_sha256": smoke.digest(scope), "native_tree_sha256": smoke.digest(tree),
        "model_projection_sha256": smoke.digest([semantic_node(node) for node in model]),
        "native_binding_sha256": smoke.digest(list(mapping.items())),
        "native_nodes": len(tree["nodes"]), "projected_native_nodes": len(native),
        "transmitted_nodes": len(model), "native_truncated": tree["truncated"],
        "model_complete": snapshot["complete"], "native_binding_verified": True,
        "focus_and_bounds_compared": False,
    }


def validate_binding(binding, snapshot, scope):
    require(isinstance(binding, dict) and set(binding) == {
        "scope_sha256", "native_tree_sha256", "model_projection_sha256", "native_binding_sha256",
        "native_nodes", "projected_native_nodes", "transmitted_nodes", "native_truncated",
        "model_complete", "native_binding_verified", "focus_and_bounds_compared"}, "practice_binding_invalid")
    require(all(isinstance(binding[key], str) and HEX.fullmatch(binding[key]) for key in (
        "scope_sha256", "native_tree_sha256", "model_projection_sha256", "native_binding_sha256")),
        "practice_binding_hash_invalid")
    require(binding["scope_sha256"] == smoke.digest(scope)
            and binding["model_projection_sha256"] == smoke.digest([semantic_node(node) for node in snapshot["nodes"]])
            and binding["native_binding_verified"] is True and binding["focus_and_bounds_compared"] is False,
            "practice_binding_mismatch")
    require(all(type(binding[key]) is int for key in ("native_nodes", "projected_native_nodes", "transmitted_nodes"))
            and 0 < binding["transmitted_nodes"] == len(snapshot["nodes"]) <= binding["projected_native_nodes"]
            <= binding["native_nodes"] <= 4096 and type(binding["native_truncated"]) is bool
            and type(binding["model_complete"]) is bool and binding["model_complete"] == snapshot["complete"]
            and (not snapshot["complete"] or (not binding["native_truncated"]
                                             and binding["projected_native_nodes"] == binding["transmitted_nodes"])),
            "practice_binding_counts_invalid")


def answer_guards(text):
    outer = re.fullmatch(r"Question: [^\n]+\nAnswer:\s*(.+)", text, re.DOTALL)
    require(outer is not None, "practice_answer_format_invalid")
    answer = outer.group(1)
    blocks = re.findall(r"```([A-Za-z0-9+#-]+)\s*\n(.*?)```", answer, re.DOTALL)
    require(len(blocks) == 1 and blocks[0][0].casefold() in {
        "python", "python3", "py", "cpp", "c++", "java", "javascript", "js", "typescript", "ts", "csharp", "c#", "go", "rust", "c"},
        "practice_answer_code_missing")
    code = blocks[0][1]
    explanation = re.sub(r"```.*?```", "", answer, flags=re.DOTALL).casefold()
    require(len(explanation.split()) >= 8 and re.search(r"\b(?:index|indices|indexes)\b", explanation),
            "practice_answer_explanation_missing")
    require(re.search(r"\b(?:for|while)\b", code) and re.search(r"\breturn\b", code)
            and re.search(r"\bnums\b", code) and re.search(r"\btarget\b", code)
            and not re.search(r"\breturn\s*[\[({]\s*-?\d+\s*,\s*-?\d+\s*[\])}]", code),
            "practice_answer_implementation_inadequate")
    hash_algorithm = (re.search(r"hash|dictionary|\bdict\b|\bmap\b", explanation)
                      and re.search(r"\btarget\s*-", code))
    sorted_algorithm = ("sort" in explanation and re.search(r"two.?pointer|left.+right", explanation)
                        and "sort" in code and re.search(r"\bwhile\b", code))
    require(hash_algorithm or sorted_algorithm, "practice_answer_algorithm_missing")
    require(re.search(r"o\s*\(\s*n(?:\s*\*?\s*log\s*n)?\s*\)", explanation),
            "practice_answer_complexity_missing")
    return {"code_language": blocks[0][0].casefold(), "code_and_explanation_present": True,
            "algorithm_structure_checked": True, "independent_semantic_review_required": True}


def parse_completion(body):
    require(isinstance(body, bytes) and len(body) <= smoke.MAX_RESPONSE_BYTES, "upstream_response_too_large")
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeError:
        raise RelayFailure("upstream_invalid_utf8") from None
    parts, decoded, usage, response_model = [], [], None, None
    stopped = done = False
    for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        require(not done, "upstream_data_after_done")
        if raw == "[DONE]":
            done = True
            continue
        event = smoke.strict_json(raw)
        decoded.append(event)
        require(isinstance(event, dict) and event.get("error") is None, "upstream_inference_error")
        if "model" in event:
            model = event["model"]
            require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model)
                    and (model == MODEL or model.startswith(MODEL + "-")), "upstream_model_mismatch")
            require(response_model in (None, model), "upstream_model_changed")
            response_model = model
        if event.get("usage") is not None:
            value = event["usage"]
            require(isinstance(value, dict) and usage is None, "upstream_invalid_usage")
            usage = {key: value.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            require(all(type(count) is int and count >= 0 for count in usage.values())
                    and 0 < usage["completion_tokens"] <= MAX_COMPLETION_TOKENS
                    and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"],
                    "upstream_invalid_usage")
            details = value.get("prompt_tokens_details")
            if isinstance(details, dict) and "cached_tokens" in details:
                cached = details["cached_tokens"]
                require(type(cached) is int and 0 <= cached <= usage["prompt_tokens"], "upstream_invalid_usage")
                usage["cached_tokens"] = cached
        choices = event.get("choices", [])
        require(isinstance(choices, list) and len(choices) <= 1, "upstream_invalid_choices")
        if not choices:
            continue
        choice = choices[0]
        require(isinstance(choice, dict) and type(choice.get("index", 0)) is int
                and choice.get("index", 0) == 0, "upstream_invalid_choice")
        reason, delta = choice.get("finish_reason"), choice.get("delta")
        require(reason in (None, "stop") and not stopped, "upstream_incomplete_completion")
        require(delta is None or isinstance(delta, dict), "upstream_invalid_delta")
        delta = delta or {}
        require(delta.get("role") in (None, "assistant")
                and all(delta.get(key) is None for key in ("tool_calls", "function_call", "refusal", "audio")),
                "upstream_nontext_completion")
        value = delta.get("content")
        require(value is None or isinstance(value, str), "upstream_invalid_content")
        if value:
            parts.append(value)
        if reason == "stop":
            stopped = True
    text = "".join(parts).strip()
    require(done and stopped and text and usage is not None and response_model,
            "upstream_incomplete_response")
    require(len(text.encode()) <= MAX_ANSWER_BYTES, "practice_answer_too_large")
    return {"text": text, "usage": usage, "returned_model": response_model,
            "answer_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "answer_guards": answer_guards(text)}, decoded


def response_body(response, envelope):
    require(isinstance(response, dict) and type(response.get("schema")) is int
            and type(response.get("turn")) is int and all(response.get(key) == envelope.get(key)
            for key in ("schema", "session_id", "turn", "request_sha256")), "practice_response_mismatch")
    require(response.get("status") == 200, "practice_upstream_failed")
    require(set(response) == {"schema", "session_id", "turn", "request_sha256", "status", "body_base64"}
            and isinstance(response["body_base64"], str), "practice_response_invalid")
    try:
        body = base64.b64decode(response["body_base64"], validate=True)
    except ValueError:
        raise RelayFailure("practice_response_encoding_invalid") from None
    completed, _decoded = parse_completion(body)
    return body, completed


class PublicPracticeProvider:
    """Desktop-side loopback provider. Caller owns all native reads."""
    model = MODEL

    def __init__(self, relay_dir, *, source_tree, window_id, window_title, browser_pid,
                 page_url, user_bus_verified, owned_profile_verified):
        self.directory = RelayDirectory(relay_dir)
        self.source_tree = source_tree
        self.scope = dict(window_id=window_id, window_title=window_title, browser_pid=browser_pid,
                          page_url=page_url, user_bus_verified=user_bus_verified,
                          owned_profile_verified=owned_profile_verified)
        try:
            validate_scope(self.scope)
            require(callable(source_tree), "practice_native_reader_required")
            require(not any(self.directory.exists(turn, kind) for turn in (1, 2)
                            for kind in ("request", "response", "attempt")), "practice_directory_already_used")
        except BaseException:
            self.directory.close()
            raise
        self.token, self.session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.requests, self.answers, self.bindings = [], [], []
        self.failure = None
        self.lock, self.stop = threading.Lock(), threading.Event()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_args):
                pass

            def reply(self, status, body, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + provider.token)

            def do_GET(self):
                if self.authorized() and self.path == "/v1/models":
                    self.reply(200, json.dumps({"data": [{"id": MODEL}]}).encode())
                else:
                    self.reply(403, b'{"error":"relay refused"}')

            def do_POST(self):
                if not self.authorized() or self.path != smoke.UPSTREAM_PATH:
                    self.reply(403, b'{"error":"relay refused"}')
                    return
                try:
                    require(self.headers.get("Transfer-Encoding") is None, "practice_chunked_request_refused")
                    length = self.headers.get("Content-Length", "")
                    require(length.isdecimal() and 0 < int(length) <= MAX_REQUEST_BYTES, "practice_request_too_large")
                    raw = self.rfile.read(int(length))
                    require(len(raw) == int(length), "practice_request_incomplete")
                    self.reply(200, provider.complete(smoke.strict_json(raw)), "text/event-stream")
                except Exception as error:
                    provider.failure = safe_code(error)
                    try:
                        self.reply(502, b'{"error":"relay refused"}')
                    except OSError:
                        pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, _request, _address):
                provider.failure = "practice_local_handler_failed"

        try:
            self.server = Server(("127.0.0.1", 0), Handler)
            self.port = self.server.server_port
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except BaseException:
            self.directory.close()
            raise

    def complete(self, request):
        with self.lock:
            require(self.failure is None and not self.stop.is_set(), "practice_session_failed")
            require(not self.requests, "practice_one_request_limit")
            try:
                snapshot = validate_request(request, self.scope)
                binding = bind_native_tree(self.source_tree(), snapshot, self.scope)
                envelope = {"schema": 1, "session_id": self.session_id, "turn": 1,
                            "scope": self.scope, "source_binding": binding, "request": request,
                            "request_sha256": smoke.digest(request)}
                self.directory.write(1, "request", envelope)
                self.requests.append(copy.deepcopy(request))
                self.bindings.append(binding)
                body, completed = response_body(self.directory.wait(1, "response", self.stop), envelope)
                self.answers.append(completed["text"])
                return body
            except Exception as error:
                self.failure = safe_code(error)
                raise RelayFailure(self.failure) from None

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        with self.lock:
            self.directory.close()

    def report(self):
        return {"transport": "public_practice_owned_file_relay", "max_requests": 1,
                "relayed_requests": len(self.requests), "completed_relay_answers": len(self.answers),
                "upstream_inference_verified_here": False, "requires_controller_report": True,
                "native_binding": self.bindings[0] if self.bindings else None,
                **({"failure_code": self.failure} if self.failure else {})}


class RootPracticeRelay:
    """Controller-only boundary; one exclusively reserved attempt per run."""
    def __init__(self, relay_dir, transport, *, secret=None):
        self.directory, self.transport = RelayDirectory(relay_dir), transport
        try:
            require(not any(self.directory.exists(turn, kind) for turn in (1, 2)
                            for kind in ("response", "attempt")), "practice_directory_already_attempted")
        except BaseException:
            self.directory.close()
            raise
        self.secret = transport._key if isinstance(transport, smoke.OpenAITransport) else secret
        self.attempts, self.responses, self.failure = 0, [], None
        self.lock = threading.Lock()

    def process_next(self):
        with self.lock:
            require(self.failure is None and self.attempts == 0, "practice_one_attempt_limit")
            receipt = None
            try:
                envelope = self.directory.wait(1, "request")
                require(isinstance(envelope, dict) and set(envelope) == {
                    "schema", "session_id", "turn", "scope", "source_binding", "request", "request_sha256"}
                        and type(envelope["schema"]) is int and envelope["schema"] == 1
                        and type(envelope["turn"]) is int and envelope["turn"] == 1
                        and isinstance(envelope["session_id"], str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{16,80}", envelope["session_id"]), "practice_envelope_invalid")
                request, scope = envelope["request"], envelope["scope"]
                require(envelope["request_sha256"] == smoke.digest(request), "practice_request_hash_mismatch")
                snapshot = validate_request(request, scope)
                validate_binding(envelope["source_binding"], snapshot, scope)
                forwarded = dict(request, max_completion_tokens=MAX_COMPLETION_TOKENS,
                                 stream_options={"include_usage": True}, store=False)
                body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode()
                require(len(body) <= MAX_REQUEST_BYTES, "practice_upstream_request_too_large")
                require(not self.secret or self.secret not in json.dumps(forwarded, ensure_ascii=False),
                        "practice_credential_in_request")
                receipt = {key: envelope[key] for key in ("schema", "session_id", "turn", "request_sha256")}
                self.directory.write(1, "attempt", receipt)
                self.attempts = 1
                data, request_id = self.transport.post(body)
                if self.secret:
                    require(self.secret.encode("ascii") not in data
                            and (request_id is None or self.secret not in request_id), "practice_credential_in_response")
                completed, decoded = parse_completion(data)
                require(not self.secret or (self.secret not in completed["text"]
                        and self.secret not in json.dumps(decoded, ensure_ascii=False)), "practice_credential_in_response")
                response = dict(receipt, status=200, body_base64=base64.b64encode(data).decode("ascii"))
                self.directory.write(1, "response", response)
                completed.update(http_status=200, request_sha256=envelope["request_sha256"],
                                 messages_sha256=smoke.digest(request["messages"]),
                                 response_body_sha256=hashlib.sha256(data).hexdigest(),
                                 source_binding=envelope["source_binding"])
                self.responses.append(completed)
                return response
            except Exception as error:
                self.failure = safe_code(error)
                if receipt is not None and self.attempts and not self.directory.exists(1, "response"):
                    self.directory.write(1, "response", dict(receipt, status=502, failure_code=self.failure))
                raise RelayFailure(self.failure) from None

    def report(self):
        return {"schema": 1, "test": "public_two_sum_native_gtk_file_relay",
                "success": len(self.responses) == 1 and self.failure is None,
                "external_ai": isinstance(self.transport, smoke.OpenAITransport), "requested_model": MODEL,
                "upstream_post_attempts": self.attempts, "max_post_attempts": 1, "retries": 0,
                "max_completion_tokens_per_request": MAX_COMPLETION_TOKENS,
                "completed_upstream_answers": len(self.responses), "streaming_latency_tested": False,
                "native_and_panel_validation": "requires_separate_desktop_UI_report",
                "independent_semantic_review_required": True,
                "completions": [{key: value for key, value in response.items() if key != "text"}
                                for response in self.responses],
                **({"failure_code": self.failure} if self.failure else {})}

    def close(self):
        self.directory.close()


def self_test():
    """Owned loopback/files and fake upstream only; no native/API/key reads."""
    identity = json.dumps([":1.77", "/org/a11y/atspi/accessible/public_fixture"], separators=(",", ":"))
    scope = {"page_url": PUBLIC_URL, "window_id": identity,
             "window_title": "Two Sum - LeetCode - Google Chrome", "browser_pid": 42,
             "user_bus_verified": True, "owned_profile_verified": True}
    question = ("1. Two Sum\nGiven an array of integers nums and an integer target, return indices "
                "of the two numbers such that they add up to target. You may not use the same "
                "element twice.\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]")
    window = {"source": "atspi", "app": "Google Chrome", "pid": 42, "id": identity}
    snapshot = {"kind": "snapshot", "schema": 1, "revision": 1, "window": window,
                "complete": True, "reason": "initial", "nodes": [
                    {"id": 1, "parent": None, "position": 0, "role": "frame"},
                    {"id": 2, "parent": 1, "position": 0, "role": "paragraph", "text": question}]}
    source = {"schema": 1, "source": "atspi", "app": window["app"], "pid": 42,
              "window_id": identity, "truncated": False, "nodes": [
                  {"id": 1, "parent": None, "stable_id": identity, "role": "frame", "name": scope["window_title"]},
                  {"id": 2, "parent": 1, "stable_id": "native:toolbar", "role": "toolbar", "name": "Browser toolbar"},
                  {"id": 3, "parent": 2, "stable_id": "native:toolbar:child", "role": "entry", "text": "not question data"},
                  {"id": 4, "parent": 1, "stable_id": "native:paragraph", "role": "paragraph", "text": question}]}

    def request_for(value=snapshot):
        return {"model": MODEL, "stream": True, "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": smoke.PREFIX + json.dumps(value, ensure_ascii=False, separators=(",", ":"))}]}

    answer = ("Question: How can I return the two distinct indices whose values sum to the target?\n"
              "Answer: Keep a hash map from each value to its index. Look for the complement before "
              "recording the current value, so the same element cannot be reused.\n"
              "```python\nclass Solution:\n    def twoSum(self, nums, target):\n"
              "        seen = {}\n        for index, value in enumerate(nums):\n"
              "            complement = target - value\n            if complement in seen:\n"
              "                return [seen[complement], index]\n            seen[value] = index\n"
              "        return []\n```\nTime O(n), auxiliary space O(n), where n is the array length.")

    def sse(parts=None, *, model=MODEL, reason="stop", usage=None, done=True, extra=None):
        events = [{"model": model, "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}]}
                  for value in (parts if parts is not None else [answer])]
        events.append({"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
        events.append({"model": model, "choices": [], "usage": usage or {
            "prompt_tokens": 300, "completion_tokens": 180, "total_tokens": 480,
            "prompt_tokens_details": {"cached_tokens": 100}}})
        if extra is not None:
            events.insert(0, extra)
        return b"".join(b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"
                        for event in events) + (b"data: [DONE]\n\n" if done else b"")

    def rejected(callback, expected=None):
        try:
            callback()
        except RelayFailure as error:
            require(expected is None or str(error) == expected, "self_test_unexpected_failure")
        else:
            raise RelayFailure("self_test_rejection_missing")

    request = request_for()
    validated = validate_request(request, scope)
    binding = bind_native_tree(source, validated, scope)
    validate_binding(binding, validated, scope)
    require(parse_completion(sse())[0]["text"] == answer, "self_test_completion_mismatch")

    # Data/topology matches the same native tree despite a real focus/layout
    # change between the CLI read and the independent verification read.
    layout = copy.deepcopy(snapshot)
    layout["nodes"][1].update(focused=True, bounds=[10, 20, 300, 40])
    bind_native_tree(source, validate_request(request_for(layout), scope), scope)
    partial = copy.deepcopy(snapshot)
    partial["complete"] = False
    partial["nodes"].append({"id": 3, "parent": 1, "position": 1, "role": "label", "text": "Public suffix"})
    longer = copy.deepcopy(source)
    longer["nodes"].append({"id": 5, "parent": 1, "stable_id": "native:last", "role": "label", "text": "Public suffix with more text"})
    bind_native_tree(longer, validate_request(request_for(partial), scope), scope)
    bad_partial = copy.deepcopy(partial)
    bad_partial["nodes"][-1]["text"] = "Unrelated content"
    rejected(lambda: bind_native_tree(longer, bad_partial, scope), "practice_native_text_mismatch")

    for key, value in (("page_url", "https://leetcode.com/problems/other/"),
                       ("user_bus_verified", False), ("owned_profile_verified", False),
                       ("browser_pid", True), ("window_id", '["not-a-unique-bus","/"]'),
                       ("window_title", "Other private window")):
        changed = dict(scope, **{key: value})
        rejected(lambda changed=changed: validate_scope(changed))
    for mutation in (
        lambda value: value.update(tools=[]),
        lambda value: value.update(model="another-model"),
        lambda value: value["messages"].append({"role": "assistant", "content": "unapproved history"}),
        lambda value: value["messages"][0].update(content="different instructions"),
        lambda value: value["messages"][1].update(content=smoke.PREFIX + '{"schema":1,"schema":2}'),
    ):
        changed = copy.deepcopy(request)
        mutation(changed)
        rejected(lambda changed=changed: validate_request(changed, scope))
    for mutation in (
        lambda value: value["window"].update(pid=43),
        lambda value: value["nodes"][1].update(id=1),
        lambda value: value["nodes"][1].update(parent=2),
        lambda value: value["nodes"][1].update(role="password text"),
        lambda value: value["nodes"][1].update(text="Only a copied example [0,1]"),
        lambda value: value.update(revision=True),
    ):
        changed = copy.deepcopy(snapshot)
        mutation(changed)
        rejected(lambda changed=changed: validate_request(request_for(changed), scope))
    for mutation in (
        lambda value: value.update(pid=43),
        lambda value: value["nodes"][3].update(text=question + "unrelated data"),
        lambda value: value["nodes"][3].update(parent=2),
        lambda value: value["nodes"][3].update(stable_id=identity),
        lambda value: value["nodes"][3].update(protected=True),
    ):
        changed = copy.deepcopy(source)
        mutation(changed)
        rejected(lambda changed=changed: bind_native_tree(changed, snapshot, scope))
    changed_binding = dict(binding, model_projection_sha256="0" * 64)
    rejected(lambda: validate_binding(changed_binding, snapshot, scope))
    rejected(lambda: answer_guards("Question: Two Sum?\nAnswer: [0,1]"))
    rejected(lambda: answer_guards(answer.replace("return [seen[complement], index]", "return [0, 1]")))
    rejected(lambda: parse_completion(sse(done=False)))
    rejected(lambda: parse_completion(sse(reason="length")))
    rejected(lambda: parse_completion(sse(model="other-model")))
    rejected(lambda: parse_completion(sse(usage={"prompt_tokens": 300, "completion_tokens": 2049, "total_tokens": 2349})))
    rejected(lambda: parse_completion(sse() + b'data: {}\n\n'))

    class FakeTransport:
        def __init__(self, payload=None, request_id=None, failure=False):
            self.calls, self.payload, self.request_id, self.failure = 0, payload or sse(), request_id, failure

        def post(self, body):
            self.calls += 1
            sent = smoke.strict_json(body)
            require(sent["messages"] == request["messages"] and sent["model"] == MODEL
                    and sent["max_completion_tokens"] == 2048 and sent["store"] is False
                    and sent["stream_options"] == {"include_usage": True}
                    and set(sent) == set(request) | {"max_completion_tokens", "store", "stream_options"},
                    "self_test_messages_changed")
            if self.failure:
                raise OSError("not reportable synthetic transport detail")
            return self.payload, self.request_id

    # Actual loopback HTTP and the same atomic files used by the live bridge.
    with tempfile.TemporaryDirectory(prefix="floaty-public-relay-selftest-") as name:
        directory = Path(name)
        provider = PublicPracticeProvider(directory, source_tree=lambda: copy.deepcopy(source), **scope)
        transport = FakeTransport()
        controller = RootPracticeRelay(directory, transport)
        results = []

        def send():
            connection = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=10)
            try:
                connection.request("POST", smoke.UPSTREAM_PATH,
                    body=json.dumps(request, ensure_ascii=False).encode(),
                    headers={"Authorization": "Bearer " + provider.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                results.append((response.status, response.read(smoke.MAX_RESPONSE_BYTES + 1)))
            finally:
                connection.close()

        thread = threading.Thread(target=send)
        try:
            thread.start()
            response = controller.process_next()
            thread.join(timeout=10)
            require(not thread.is_alive() and results == [(200, sse())]
                    and provider.answers == [answer] and provider.requests == [request]
                    and transport.calls == 1 and controller.attempts == 1,
                    "self_test_relay_roundtrip_failed")
            envelope = controller.directory.read(1, "request")
            wrong = dict(response, request_sha256="0" * 64)
            rejected(lambda: response_body(wrong, envelope), "practice_response_mismatch")
            rejected(lambda: response_body(dict(response, turn=True), envelope), "practice_response_mismatch")
            rejected(lambda: provider.complete(request), "practice_one_request_limit")
            rejected(controller.process_next, "practice_one_attempt_limit")
            rejected(lambda: RootPracticeRelay(directory, transport), "practice_directory_already_attempted")
            require(transport.calls == 1 and not controller.directory.exists(2, "request"), "self_test_attempt_cap_failed")
        finally:
            provider.close()
            controller.close()

    # Literal comments, JSON escapes, content fragments and response headers
    # must never carry the controller credential back to the desktop.
    secret = "synthetic-only-credential"
    escaped = "".join("\\u%04x" % ord(character) for character in secret)
    extra = b'data: {"choices":[],"diagnostic":"' + escaped.encode() + b'"}\n\n'
    secret_answer = answer.replace("Question: ", "Question: " + secret + " ")
    split = secret_answer.index(secret) + len(secret) // 2
    failures = [FakeTransport(payload=b": " + secret.encode() + b"\n\n" + sse()),
                FakeTransport(payload=extra + sse()),
                FakeTransport(payload=sse([secret_answer[:split], secret_answer[split:]])),
                FakeTransport(request_id="req_" + secret), FakeTransport(failure=True)]
    for transport in failures:
        with tempfile.TemporaryDirectory(prefix="floaty-public-refusal-") as name:
            controller = RootPracticeRelay(Path(name), transport, secret=secret)
            envelope = {"schema": 1, "session_id": "synthetic_session_123456", "turn": 1,
                        "scope": scope, "source_binding": binding, "request": request,
                        "request_sha256": smoke.digest(request)}
            try:
                controller.directory.write(1, "request", envelope)
                rejected(controller.process_next)
                response = controller.directory.read(1, "response")
                require(response["status"] == 502 and "body_base64" not in response
                        and secret not in json.dumps(controller.report()) and transport.calls == 1,
                        "self_test_failure_escaped")
                rejected(controller.process_next, "practice_one_attempt_limit")
                require(transport.calls == 1, "self_test_failed_request_retried")
            finally:
                controller.close()
    return True


def main():
    parser = smoke.QuietArgumentParser(description=__doc__)
    parser.add_argument("--relay-dir", type=Path)
    parser.add_argument("--key-fd", type=int)
    parser.add_argument("--https-proxy")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    relay = None
    report = {"schema": 1, "success": False, "test": "public_two_sum_native_gtk_file_relay",
              "external_ai": not args.self_test, "upstream_post_attempts": 0}
    try:
        if args.self_test:
            require(args.key_fd is None and args.relay_dir is None and args.https_proxy is None,
                    "self_test_must_not_receive_credentials_or_routes")
            self_test()
            report.update(success=True, self_test=True)
        else:
            require(args.relay_dir is not None, "practice_directory_required")
            proxy = smoke.parse_https_proxy(args.https_proxy)
            key = smoke.read_key(args.key_fd)
            relay = RootPracticeRelay(args.relay_dir, smoke.OpenAITransport(key, proxy), secret=key)
            relay.process_next()
            report.update(relay.report())
    except KeyboardInterrupt:
        report["failure_code"] = "cancelled"
    except Exception as error:
        report["failure_code"] = safe_code(error)
    finally:
        if relay is not None:
            report.update(relay.report())
            relay.close()
        print(json.dumps(report, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
