#!/usr/bin/env python3
"""Opt-in, two-turn file relay for the owned generated native browser lab.

The HP-side FileRelayProvider has only a loopback listener and a random local
token. It publishes numbered request files and waits at most 180 seconds for
matching responses. The root-side CLI validates transferred requests, uses the
audited OpenAI transport, and publishes the original SSE as base64. The caller
transfers files; this helper has no remote-file or arbitrary-endpoint support.
Only the root-side process reads the real key, through hidden TTY/anonymous FD.
No answers from the oracle are added to the actual CLI/model messages.
File transfers make this unsuitable for latency measurement. The production
CLI's independent HTTP timeout still bounds successful answer completion.

  python3 screen_model_relay.py --relay-dir /owned/private/relay --key-fd FD
  python3 screen_model_relay.py --self-test
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
import stat
import tempfile
import threading
import time

import openai_live_smoke as smoke


ROOT = Path(__file__).resolve().parents[2]
MAX_REQUEST_BYTES = 64_000
MAX_REQUEST_FILE_BYTES = MAX_REQUEST_BYTES + 8192
MAX_RESPONSE_FILE_BYTES = 4 * ((smoke.MAX_RESPONSE_BYTES + 2) // 3) + 2048
WAIT_SECONDS = 180
require = smoke.require
RelayFailure = smoke.SmokeFailure


def contract():
    html = (ROOT / "tests/fixtures/screen-reading-lab.html").read_bytes()
    raw = (ROOT / "tests/fixtures/screen-reading-lab-cases.json").read_bytes()
    oracle = smoke.strict_json(raw)
    cases = {case["id"]: case for case in oracle["cases"]}
    identity = oracle["harness"]["stable_identity_sequence"]
    return {
        "fixture_sha256": hashlib.sha256(html).hexdigest(),
        "oracle_sha256": hashlib.sha256(raw).hexdigest(),
        "title_prefix": oracle["harness"]["window_title_prefix"],
        "before": identity["before_question_marker"],
        "after": identity["after_question_marker"],
        "anchor": identity["anchor_marker"],
        "answers": [cases[name]["initial"]["expected"]["accessibility_only"]["exact"]
                    for name in ("baseline", "followup")],
        "passwords": cases["password"]["initial"]["absent_markers"],
        "system": (ROOT / "crates/socket_stream_core/prompts/practice-system-v3.md").read_text(encoding="utf-8"),
    }


def safe_code(error):
    code = str(error) if isinstance(error, RelayFailure) else "relay_failed"
    return code if re.fullmatch(r"[a-z0-9_]{1,100}", code) else "relay_failed"


class RelayDirectory:
    """Fixed filenames beneath an already owned, private directory FD."""
    def __init__(self, path):
        path = Path(path)
        require(path.is_absolute(), "relay_directory_must_be_absolute")
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            self.close()
            raise RelayFailure("relay_directory_not_private_owned")

    @staticmethod
    def name(turn, kind):
        require(type(turn) is int and turn in (1, 2) and kind in ("request", "response", "attempt"),
                "relay_filename_invalid")
        return f"turn-{turn}.{kind}.json"

    def exists(self, turn, kind):
        try:
            os.stat(self.name(turn, kind), dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def read(self, turn, kind):
        name = self.name(turn, kind)
        limit = MAX_RESPONSE_FILE_BYTES if kind == "response" else MAX_REQUEST_FILE_BYTES
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077 and info.st_size <= limit, "relay_file_not_private_regular")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(limit + 1)
            require(len(data) <= limit, "relay_file_too_large")
            return smoke.strict_json(data)
        finally:
            os.close(fd)

    def write(self, turn, kind, value):
        name = self.name(turn, kind)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        limit = MAX_RESPONSE_FILE_BYTES if kind == "response" else MAX_REQUEST_FILE_BYTES
        require(len(data) <= limit, "relay_file_too_large")
        temporary = "." + name + "." + secrets.token_hex(8)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(data)
                output.flush()
                os.fsync(fd)
            # Atomic publication without overwriting an existing turn or link.
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
        finally:
            os.close(fd)
            os.unlink(temporary, dir_fd=self.fd)

    def wait(self, turn, kind, stop=None):
        deadline = time.monotonic() + WAIT_SECONDS
        stop = stop or threading.Event()
        while time.monotonic() < deadline:
            require(not stop.is_set(), "relay_closed")
            value = self.read(turn, kind)
            if value is not None:
                return value
            stop.wait(.1)
        raise RelayFailure("relay_file_timeout")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def validate_scope(scope, approved):
    require(isinstance(scope, dict) and set(scope) == {
        "window_id", "window_title", "private_bus_verified", "generated_only", "fixture_sha256", "oracle_sha256",
    }, "relay_scope_invalid")
    require(scope["private_bus_verified"] is True and scope["generated_only"] is True,
            "relay_private_scope_unverified")
    require(scope["fixture_sha256"] == approved["fixture_sha256"]
            and scope["oracle_sha256"] == approved["oracle_sha256"], "relay_fixture_hash_mismatch")
    require(isinstance(scope["window_title"], str) and len(scope["window_title"]) <= 512
            and scope["window_title"].startswith(approved["title_prefix"]), "relay_window_title_mismatch")
    require(isinstance(scope["window_id"], str) and len(scope["window_id"]) <= 4096, "relay_window_id_invalid")
    identity = smoke.strict_json(scope["window_id"])
    require(isinstance(identity, list) and len(identity) == 2
            and isinstance(identity[0], str) and re.fullmatch(r":[0-9]+\.[0-9]+", identity[0])
            and isinstance(identity[1], str) and identity[1].startswith("/"), "relay_window_id_invalid")


def node_map(nodes):
    require(isinstance(nodes, list) and 0 < len(nodes) <= 800, "relay_nodes_invalid")
    result = {}
    positions = set()
    for node in nodes:
        require(isinstance(node, dict) and {"id", "parent", "position", "role"} <= set(node)
                and set(node) <= {"id", "parent", "position", "role", "name", "text", "focused", "bounds", "image"},
                "relay_node_fields_invalid")
        require(type(node["id"]) is int and 0 < node["id"] < 2**64 and node["id"] not in result
                and (node["parent"] is None or type(node["parent"]) is int)
                and type(node["position"]) is int and 0 <= node["position"] < 800, "relay_node_identity_invalid")
        require(isinstance(node["role"], str) and 0 < len(node["role"]) <= 256
                and "password" not in node["role"].casefold() and "protected" not in node["role"].casefold(),
                "relay_protected_node")
        require(all(isinstance(node.get(key, ""), str) for key in ("name", "text")), "relay_node_text_invalid")
        require(all(key not in node or type(node[key]) is bool for key in ("focused", "image")), "relay_node_state_invalid")
        bounds = node.get("bounds")
        require(bounds is None or (isinstance(bounds, list) and len(bounds) == 4
                                   and all(type(value) is int for value in bounds)), "relay_node_bounds_invalid")
        position = (node["parent"], node["position"])
        require(position not in positions, "relay_node_order_invalid")
        positions.add(position)
        result[node["id"]] = node
    finished = set()
    for identity in result:
        trail = set()
        while identity is not None and identity not in finished:
            require(identity in result and identity not in trail, "relay_node_parent_invalid")
            trail.add(identity)
            identity = result[identity]["parent"]
        finished.update(trail)
    return result


def screen_update(message):
    require(message.startswith(smoke.PREFIX), "relay_screen_protocol_mismatch")
    return smoke.strict_json(message[len(smoke.PREFIX):])


def validate_question_patch(snapshot, update, before_marker, after_marker, anchor):
    """Replay one semantic question edit, including every native text alias.

    A DOM Text mutation may update both an AT-SPI paragraph and its static
    child. Count native identities, never assume one DOM edit means one node.
    This fixture permits no change outside that connected question group.
    """
    require(isinstance(snapshot, dict) and set(snapshot) == {
        "kind", "schema", "revision", "window", "complete", "reason", "nodes",
    } and snapshot["kind"] == "snapshot" and type(snapshot["schema"]) is int and snapshot["schema"] == 1
            and snapshot["complete"] is True and type(snapshot["revision"]) is int
            and snapshot["revision"] == 1 and snapshot["reason"] == "initial", "relay_initial_snapshot_invalid")
    nodes = node_map(snapshot.get("nodes"))
    require(isinstance(update, dict) and set(update) == {
        "kind", "schema", "base_revision", "revision", "window", "added", "updated", "moved", "removed",
    } and update["kind"] == "patch" and type(update["schema"]) is int and update["schema"] == 1
            and type(update["base_revision"]) is int and update["base_revision"] == 1
            and type(update["revision"]) is int and update["revision"] == 2
            and update["window"] == snapshot.get("window") and update["added"] == []
            and update["moved"] == [] and update["removed"] == []
            and isinstance(update["updated"], list), "relay_question_patch_invalid")
    question_ids = {identity for identity, node in nodes.items()
                    if any(before_marker in node.get(key, "") for key in ("name", "text"))}
    require(question_ids, "relay_question_group_missing")
    roots = set()
    for identity in question_ids:
        node = nodes[identity]
        require(node["role"].casefold() in {"paragraph", "static", "static text", "text", "label"}
                and all(anchor not in node.get(key, "") for key in ("name", "text")),
                "relay_question_group_invalid")
        current = node["parent"]
        while current is not None and current not in question_ids:
            current = nodes[current]["parent"]
        if current is None:
            roots.add(identity)
    require(len(roots) == 1, "relay_question_group_ambiguous")
    changed_ids = []
    for changed in update["updated"]:
        require(isinstance(changed, dict) and type(changed.get("id")) is int,
                "relay_patch_identity_invalid")
        changed_ids.append(changed["id"])
    require(len(changed_ids) == len(set(changed_ids)) and set(changed_ids) == question_ids,
            "relay_changed_question_ids_mismatch")
    replayed = dict(nodes)
    for changed in update["updated"]:
        previous = nodes[changed["id"]]
        require(all(changed.get(key, default) == previous.get(key, default)
                    for key, default in (("parent", None), ("position", None), ("role", None),
                                         ("focused", False), ("image", False))),
                "relay_question_identity_changed")
        for key in ("name", "text"):
            old, new = previous.get(key, ""), changed.get(key, "")
            require(isinstance(new, str), "relay_node_text_invalid")
            if before_marker in old:
                require(after_marker in new and before_marker not in new and anchor not in new,
                        "relay_question_alias_not_updated")
            else:
                require(new == old, "relay_unrelated_question_field_changed")
        replayed[changed["id"]] = changed
    replayed = node_map(list(replayed.values()))
    current = json.dumps(list(replayed.values()), ensure_ascii=False)
    require(after_marker in current and anchor in current and before_marker not in current,
            "relay_patched_screen_invalid")
    serialized_patch = json.dumps(update, ensure_ascii=False)
    require(anchor not in serialized_patch and before_marker not in serialized_patch,
            "relay_unchanged_content_repeated")
    return replayed


def validate_request(request, turn, scope, approved, prior_request=None, prior_answer=None):
    require(isinstance(request, dict) and set(request) == {"model", "messages", "stream"}
            and request["model"] == smoke.MODEL and request["stream"] is True, "relay_request_fields_invalid")
    messages = request["messages"]
    roles = ["system", "user"] if turn == 1 else ["system", "user", "assistant", "user"]
    require(isinstance(messages, list) and len(messages) == len(roles)
            and all(isinstance(message, dict) and set(message) == {"role", "content"}
                    and message["role"] == role and isinstance(message["content"], str)
                    for message, role in zip(messages, roles)), "relay_history_invalid")
    require(messages[0]["content"] == approved["system"], "relay_system_mismatch")
    if turn == 2:
        require(prior_request is not None and prior_answer is not None
                and messages[:2] == prior_request["messages"]
                and messages[2]["content"] == prior_answer, "relay_acknowledged_history_mismatch")
    update = screen_update(messages[-1]["content"])
    snapshot = update if turn == 1 else screen_update(prior_request["messages"][-1]["content"])
    require(isinstance(snapshot, dict) and set(snapshot) == {
        "kind", "schema", "revision", "window", "complete", "reason", "nodes",
    } and snapshot["kind"] == "snapshot" and type(snapshot["schema"]) is int and snapshot["schema"] == 1
            and type(snapshot["revision"]) is int and snapshot["revision"] == 1
            and snapshot["complete"] is True and snapshot["reason"] == "initial", "relay_initial_snapshot_invalid")
    window = snapshot["window"]
    require(isinstance(window, dict) and set(window) == {"source", "app", "pid", "id"}
            and window["source"] == "atspi" and window["id"] == scope["window_id"]
            and isinstance(window["app"], str) and len(window["app"]) <= 512
            and type(window["pid"]) is int and window["pid"] > 0, "relay_window_mismatch")
    node_map(snapshot["nodes"])
    flattened = json.dumps(snapshot, ensure_ascii=False)
    require(approved["before"] in flattened and approved["anchor"] in flattened
            and approved["after"] not in flattened, "relay_baseline_markers_missing")
    if turn == 2:
        validate_question_patch(snapshot, update, approved["before"], approved["after"], approved["anchor"])
        require(approved["anchor"] not in messages[-1]["content"]
                and approved["before"] not in messages[-1]["content"], "relay_unchanged_content_repeated")
    decoded = json.dumps([messages, snapshot, update], ensure_ascii=False)
    require(not any(value in decoded for value in approved["passwords"]), "relay_password_leaked")
    require(not any(answer in json.dumps(update, ensure_ascii=False) for answer in approved["answers"]),
            "relay_oracle_answer_in_screen")


def response_body(response, envelope, approved):
    require(isinstance(response, dict) and response.get("schema") == 1
            and response.get("session_id") == envelope["session_id"] and response.get("turn") == envelope["turn"]
            and response.get("request_sha256") == envelope["request_sha256"], "relay_response_mismatch")
    if response.get("status") != 200:
        raise RelayFailure("relay_upstream_failed")
    require(set(response) == {"schema", "session_id", "turn", "request_sha256", "status", "body_base64"}
            and isinstance(response["body_base64"], str), "relay_response_invalid")
    try:
        data = base64.b64decode(response["body_base64"], validate=True)
    except ValueError:
        raise RelayFailure("relay_response_encoding_invalid") from None
    completed = smoke.parse_completion(data, envelope["turn"] - 1,
                                       expected_answer=approved["answers"][envelope["turn"] - 1])
    return data, completed


class FileRelayProvider:
    """HP-side provider: no real credential and no external network calls."""
    model = smoke.MODEL

    def __init__(self, relay_dir, *, window_id, window_title, private_bus_verified):
        self.directory = RelayDirectory(relay_dir)
        self.approved = contract()
        self.scope = {"window_id": window_id, "window_title": window_title,
                      "private_bus_verified": private_bus_verified, "generated_only": True,
                      **{key: self.approved[key] for key in ("fixture_sha256", "oracle_sha256")}}
        try:
            validate_scope(self.scope, self.approved)
            require(not any(self.directory.exists(turn, kind) for turn in (1, 2)
                            for kind in ("request", "response", "attempt")), "relay_directory_already_used")
        except Exception:
            self.directory.close()
            raise
        self.token, self.session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.requests, self.answers = [], []
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
                    self.reply(200, json.dumps({"data": [{"id": provider.model}]}).encode())
                else:
                    self.reply(403, b'{"error":"relay refused"}')

            def do_POST(self):
                if not self.authorized() or self.path != smoke.UPSTREAM_PATH:
                    self.reply(403, b'{"error":"relay refused"}')
                    return
                try:
                    require(self.headers.get("Transfer-Encoding") is None, "relay_chunked_request_refused")
                    length = self.headers.get("Content-Length", "")
                    require(length.isdecimal() and 0 < int(length) <= MAX_REQUEST_BYTES, "relay_request_too_large")
                    body = self.rfile.read(int(length))
                    require(len(body) == int(length), "relay_request_incomplete")
                    self.reply(200, provider.complete(smoke.strict_json(body)), "text/event-stream")
                except Exception as error:
                    provider.failure = safe_code(error)
                    try:
                        self.reply(502, b'{"error":"relay refused"}')
                    except OSError:
                        pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, _request, _address):
                provider.failure = "relay_local_handler_failed"

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def complete(self, request):
        with self.lock:
            require(self.failure is None and not self.stop.is_set(), "relay_session_failed")
            require(len(self.requests) < 2, "relay_two_request_limit")
            turn = len(self.requests) + 1
            validate_request(request, turn, self.scope, self.approved,
                             self.requests[0] if turn == 2 else None, self.answers[0] if turn == 2 else None)
            envelope = {"schema": 1, "session_id": self.session_id, "turn": turn,
                        "scope": self.scope, "request": request, "request_sha256": smoke.digest(request)}
            self.directory.write(turn, "request", envelope)
            self.requests.append(copy.deepcopy(request))
            data, completed = response_body(self.directory.wait(turn, "response", self.stop), envelope, self.approved)
            self.answers.append(completed["text"])
            return data

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        with self.lock:
            self.directory.close()

    def report(self):
        return {"transport": "owned_file_relay", "relayed_requests": len(self.requests),
                "completed_relay_answers": len(self.answers), "upstream_inference_verified_here": False,
                "requires_root_relay_report": True,
                **({"failure_code": self.failure} if self.failure else {})}


class RootRelaySession:
    """Root-side validator; transports only approved requests, without retries."""
    def __init__(self, relay_dir, transport, *, secret=None):
        self.directory, self.transport = RelayDirectory(relay_dir), transport
        try:
            require(not any(self.directory.exists(turn, kind) for turn in (1, 2)
                            for kind in ("response", "attempt")), "relay_directory_already_attempted")
        except Exception:
            self.directory.close()
            raise
        self.approved = contract()
        self.secret = transport._key if isinstance(transport, smoke.OpenAITransport) else secret
        self.attempts, self.requests, self.responses = 0, [], []
        self.session_id, self.scope, self.failure = None, None, None
        self.lock = threading.Lock()

    def process_next(self):
        with self.lock:
            require(self.failure is None and self.attempts < 2, "relay_two_attempt_limit")
            turn = self.attempts + 1
            envelope = self.directory.wait(turn, "request")
            require(isinstance(envelope, dict) and set(envelope) == {
                "schema", "session_id", "turn", "scope", "request", "request_sha256",
            } and type(envelope["schema"]) is int and envelope["schema"] == 1
                    and type(envelope["turn"]) is int and envelope["turn"] == turn
                    and isinstance(envelope["session_id"], str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{16,80}", envelope["session_id"]), "relay_envelope_invalid")
            require(envelope["request_sha256"] == smoke.digest(envelope["request"]), "relay_request_hash_mismatch")
            validate_scope(envelope["scope"], self.approved)
            require(self.session_id in (None, envelope["session_id"])
                    and self.scope in (None, envelope["scope"]), "relay_session_scope_changed")
            request = envelope["request"]
            validate_request(request, turn, envelope["scope"], self.approved,
                             self.requests[0] if turn == 2 else None,
                             self.responses[0]["text"] if turn == 2 else None)
            forwarded = dict(request, max_completion_tokens=smoke.MAX_COMPLETION_TOKENS,
                             stream_options={"include_usage": True}, store=False)
            body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode()
            require(len(body) <= MAX_REQUEST_BYTES, "relay_upstream_request_too_large")
            receipt = {"schema": 1, "session_id": envelope["session_id"], "turn": turn,
                       "request_sha256": envelope["request_sha256"]}
            self.directory.write(turn, "attempt", receipt)
            self.attempts += 1  # Persistent reservation precedes external I/O.
            try:
                data, request_id = self.transport.post(body)
                if self.secret:
                    require(self.secret.encode("ascii") not in data, "relay_credential_in_response")
                    require(request_id is None or self.secret not in request_id, "relay_credential_in_response")
                    # Reject even a JSON-escaped credential echoed by upstream.
                    decoded = [smoke.strict_json(line[5:].strip()) for line in data.decode("utf-8").splitlines()
                               if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]
                    require(self.secret not in json.dumps(decoded, ensure_ascii=False), "relay_credential_in_response")
                response = dict(receipt, status=200, body_base64=base64.b64encode(data).decode("ascii"))
                _data, completed = response_body(response, envelope, self.approved)
                require(not self.secret or self.secret not in completed["text"], "relay_credential_in_response")
                self.directory.write(turn, "response", response)
            except Exception as error:
                self.failure = safe_code(error)
                self.directory.write(turn, "response", dict(receipt, status=502, failure_code=self.failure))
                raise RelayFailure(self.failure) from None
            self.session_id, self.scope = envelope["session_id"], envelope["scope"]
            self.requests.append(copy.deepcopy(request))
            completed.update(request_id=request_id, messages_sha256=smoke.digest(request["messages"]))
            self.responses.append(completed)
            return response

    def report(self):
        return {"schema": 1, "test": "generated_native_screen_file_relay",
                "success": len(self.responses) == 2 and self.failure is None,
                "external_ai": isinstance(self.transport, smoke.OpenAITransport),
                "requested_model": smoke.MODEL, "upstream_post_attempts": self.attempts,
                "max_post_attempts": 2, "max_completion_tokens_per_request": smoke.MAX_COMPLETION_TOKENS,
                "retries": 0, "completed_upstream_answers": len(self.responses),
                "relay_wait_seconds": WAIT_SECONDS, "streaming_latency_tested": False,
                "native_validation": "requires_separate_HP_browser_lab_report",
                "completions": [{key: value for key, value in response.items() if key != "text"}
                                for response in self.responses],
                **({"failure_code": self.failure} if self.failure else {})}

    def close(self):
        self.directory.close()


def self_test():
    """Real loopback/files, fake upstream; no desktop, key, or external network."""
    approved = contract()
    window_id = json.dumps([":1.77", "/org/a11y/atspi/accessible/fixture"], separators=(",", ":"))
    window = {"source": "atspi", "app": "Google Chrome", "pid": 4242, "id": window_id}
    initial = {"kind": "snapshot", "schema": 1, "revision": 1, "window": window,
               "complete": True, "reason": "initial", "nodes": [
                   {"id": 91, "parent": None, "position": 0, "role": "frame"},
                   {"id": 5, "parent": 91, "position": 0, "role": "document web", "name": approved["title_prefix"]},
                   {"id": 27, "parent": 5, "position": 0, "role": "heading", "text": "Arithmetic and session context"},
                   {"id": 15, "parent": 5, "position": 1, "role": "paragraph", "text": approved["before"]},
                   {"id": 16, "parent": 15, "position": 0, "role": "static", "name": approved["before"],
                    "text": approved["before"], "bounds": [0, 0, 100, 20]},
                   {"id": 33, "parent": 5, "position": 2, "role": "static", "text": "Session code: " + approved["anchor"]},
               ]}
    changed = dict(initial["nodes"][3], text=approved["after"])
    patch = {"kind": "patch", "schema": 1, "base_revision": 1, "revision": 2, "window": window,
             "added": [], "updated": [changed, dict(initial["nodes"][4], name=approved["after"],
                                                     text=approved["after"], bounds=[0, 0, 200, 40])],
             "moved": [], "removed": []}
    first = {"model": smoke.MODEL, "stream": True, "messages": [
        {"role": "system", "content": approved["system"]},
        {"role": "user", "content": smoke.PREFIX + json.dumps(initial)},
    ]}

    class FakeTransport:
        def __init__(self, echo=None, fragmented=False, header_only=False, comment_only=False):
            self.requests, self.echo, self.fragmented = [], echo, fragmented
            self.header_only, self.comment_only = header_only, comment_only

        def post(self, body):
            request = smoke.strict_json(body)
            require(request["max_completion_tokens"] == 1024 and request["store"] is False
                    and request["stream_options"] == {"include_usage": True}, "self_test_upstream_envelope")
            self.requests.append(request)
            turn = len(self.requests)
            event = {"model": smoke.MODEL, "choices": [{"delta": {
                "content": "Question: Generated fixture question\nAnswer: " + approved["answers"][turn - 1],
            }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
            if self.echo and self.header_only:
                return ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode(), "req_" + self.echo
            if self.echo and self.comment_only:
                return (": " + self.echo + "\n\ndata: " + json.dumps(event)
                        + "\n\ndata: [DONE]\n\n").encode(), "req_relay_self_test"
            if self.echo and self.fragmented:
                midpoint = len(self.echo) // 2
                first_chunk = {"model": smoke.MODEL, "choices": [{"delta": {
                    "content": "Question: " + self.echo[:midpoint],
                }, "finish_reason": None}]}
                event["choices"][0]["delta"]["content"] = self.echo[midpoint:] + "\nAnswer: " + approved["answers"][turn - 1]
                return ("data: " + json.dumps(first_chunk) + "\n\ndata: " + json.dumps(event)
                        + "\n\ndata: [DONE]\n\n").encode(), "req_relay_self_test"
            if self.echo:
                event["debug"] = self.echo
            raw = json.dumps(event)
            if self.echo:
                raw = raw.replace(self.echo, "\\u0073" + self.echo[1:])
            return ("data: " + raw + "\n\ndata: [DONE]\n\n").encode(), "req_relay_self_test"

    def rejected(action, code=None):
        try:
            action()
        except (RelayFailure, OSError) as error:
            require(code is None or safe_code(error) == code, "self_test_wrong_rejection")
        else:
            raise RelayFailure("self_test_missing_rejection")

    def replay(snapshot, update):
        return validate_question_patch(snapshot, update, approved["before"], approved["after"], approved["anchor"])

    restored = replay(initial, patch)
    require(approved["before"] not in json.dumps(restored)
            and restored[33] == initial["nodes"][-1], "self_test_alias_replay_stale_or_anchor_changed")
    single = copy.deepcopy(initial)
    single["nodes"] = [node for node in single["nodes"] if node["id"] != 16]
    replay(single, dict(patch, updated=patch["updated"][:1]))
    for mutate in (
        lambda value: value.update(updated=value["updated"][:1]),  # Dropped changed child.
        lambda value: value.update(updated=value["updated"][1:]),  # Dropped changed parent.
        lambda value: value["updated"].append(copy.deepcopy(value["updated"][0])),
        lambda value: value["updated"].append(copy.deepcopy(initial["nodes"][-1])),
        lambda value: value["updated"].append(dict(initial["nodes"][2], text="Unrelated changed heading")),
        lambda value: value["updated"][1].update(id=999),
        lambda value: value["updated"][1].update(parent=5),
        lambda value: value["updated"][1].update(position=1),
        lambda value: value["updated"][1].update(role="button"),
        lambda value: value["updated"][1].update(focused=True),
        lambda value: value["updated"][1].update(text=approved["before"]),
        lambda value: value["updated"][1].update(text=approved["after"] + approved["anchor"]),
        lambda value: value["updated"][0].update(name="Unrelated invented label"),
        lambda value: value["updated"][1].update(bounds=[0, 0, True, 40]),
        lambda value: value.update(added=[dict(initial["nodes"][-1], id=999)]),
        lambda value: value.update(removed=[16]),
        lambda value: value.update(moved=[{"id": 16, "parent": 5, "position": 3}]),
        lambda value: value.update(base_revision=2),
        lambda value: value.update(revision=True),
        lambda value: value.update(window=dict(window, pid=999)),
    ):
        invalid = copy.deepcopy(patch)
        mutate(invalid)
        rejected(lambda: replay(initial, invalid))
    disconnected = copy.deepcopy(initial)
    disconnected["nodes"][4].update(parent=5, position=3)
    disconnected_patch = copy.deepcopy(patch)
    disconnected_patch["updated"][1].update(parent=5, position=3)
    rejected(lambda: replay(disconnected, disconnected_patch), "relay_question_group_ambiguous")
    duplicated = copy.deepcopy(initial)
    duplicated["nodes"].append(copy.deepcopy(duplicated["nodes"][3]))
    rejected(lambda: replay(duplicated, patch), "relay_node_identity_invalid")

    with tempfile.TemporaryDirectory(prefix="relay-self-test-") as temporary:
        base = Path(temporary)
        hp, control = base / "hp", base / "root"
        hp.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        provider = FileRelayProvider(hp, window_id=window_id, window_title=approved["title_prefix"], private_bus_verified=True)
        fake = FakeTransport()
        relay = RootRelaySession(control, fake)
        try:
            for turn in (1, 2):
                request = first if turn == 1 else {
                    "model": smoke.MODEL, "stream": True, "messages": first["messages"] + [
                        {"role": "assistant", "content": provider.answers[0]},
                        {"role": "user", "content": smoke.PREFIX + json.dumps(patch)},
                    ]}
                results = []

                def local_request():
                    connection = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=4)
                    try:
                        connection.request("POST", smoke.UPSTREAM_PATH, body=json.dumps(request).encode(),
                                           headers={"Authorization": "Bearer " + provider.token,
                                                    "Content-Type": "application/json"})
                        response = connection.getresponse()
                        results.append((response.status, response.read(smoke.MAX_RESPONSE_BYTES + 1)))
                    except Exception:
                        results.append((0, b""))
                    finally:
                        connection.close()

                client = threading.Thread(target=local_request, daemon=True)
                client.start()
                envelope = provider.directory.wait(turn, "request")
                relay.directory.write(turn, "request", envelope)
                response = relay.process_next()
                provider.directory.write(turn, "response", response)
                client.join(timeout=5)
                require(results == [(200, base64.b64decode(response["body_base64"]))], "self_test_relay_roundtrip")
                require(fake.requests[-1]["messages"] == request["messages"], "self_test_messages_changed")
            require(relay.report()["success"] and not relay.report()["external_ai"]
                    and len(provider.answers) == 2 and not provider.report()["upstream_inference_verified_here"],
                    "self_test_relay_report")
            rejected(relay.process_next, "relay_two_attempt_limit")
            rejected(lambda: provider.complete(first), "relay_two_request_limit")
            rejected(lambda: RootRelaySession(control, FakeTransport()), "relay_directory_already_attempted")
            require(len(fake.requests) == relay.attempts == 2, "self_test_relay_attempt_cap")

            bad_history = copy.deepcopy(provider.requests[1])
            bad_history["messages"][2]["content"] = "Unacknowledged answer"
            rejected(lambda: validate_request(bad_history, 2, provider.scope, approved, first, provider.answers[0]),
                     "relay_acknowledged_history_mismatch")
            for mutate in (
                lambda value: value.update(tools=[]),
                lambda value: value["messages"][-1].update(content=[{"type": "image_url"}]),
                lambda value: value["messages"][-1].update(content=smoke.PREFIX + '{"schema":0,' + json.dumps(initial)[1:]),
                lambda value: value["messages"][-1].update(content=smoke.PREFIX + json.dumps(dict(initial, complete=False))),
                lambda value: value["messages"][-1].update(content=smoke.PREFIX + json.dumps(dict(initial, nodes=[
                    dict(node, text=approved["passwords"][0]) if node["id"] == 27 else node for node in initial["nodes"]]))),
            ):
                invalid = copy.deepcopy(first)
                mutate(invalid)
                rejected(lambda: validate_request(invalid, 1, provider.scope, approved))

            unsafe = base / "unsafe"
            unsafe.mkdir(mode=0o700)
            directory = RelayDirectory(unsafe)
            try:
                (unsafe / "turn-1.request.json").symlink_to(control / "turn-1.request.json")
                rejected(lambda: directory.read(1, "request"))
                rejected(lambda: directory.write(1, "request", {"unexpected": True}))
                require((unsafe / "turn-1.request.json").is_symlink(), "self_test_relay_overwrote_link")
            finally:
                directory.close()

            secret = "synthetic-relay-private"
            for kind in ("escaped", "fragmented", "header", "comment"):
                private = base / ("credential-" + kind)
                private.mkdir(mode=0o700)
                failed = RootRelaySession(private, FakeTransport(echo=secret, fragmented=kind == "fragmented",
                                                                header_only=kind == "header", comment_only=kind == "comment"),
                                          secret=secret)
                try:
                    failed.directory.write(1, "request", provider.directory.read(1, "request"))
                    rejected(failed.process_next, "relay_credential_in_response")
                    sanitized = json.dumps(failed.directory.read(1, "response"))
                    require(secret not in sanitized and "body_base64" not in sanitized and failed.attempts == 1,
                            "self_test_credential_written")
                finally:
                    failed.close()
        finally:
            provider.close()
            relay.close()


def main():
    parser = smoke.QuietArgumentParser(description=__doc__)
    parser.add_argument("--relay-dir", type=Path)
    parser.add_argument("--key-fd", type=int)
    parser.add_argument("--https-proxy")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    relay = None
    report = {"schema": 1, "success": False, "test": "generated_native_screen_file_relay",
              "external_ai": not args.self_test, "upstream_post_attempts": 0}
    try:
        if args.self_test:
            require(args.key_fd is None and args.relay_dir is None, "self_test_must_not_receive_credentials_or_directory")
            self_test()
            report.update(success=True, self_test=True)
        else:
            require(args.relay_dir is not None, "relay_directory_required")
            proxy = smoke.parse_https_proxy(args.https_proxy)
            key = smoke.read_key(args.key_fd)
            relay = RootRelaySession(args.relay_dir, smoke.OpenAITransport(key, proxy), secret=key)
            relay.process_next()
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
