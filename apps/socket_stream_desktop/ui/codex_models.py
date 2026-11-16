"""Bounded, non-inference Codex App Server model discovery."""
import json
import math
import os
import queue
import subprocess
import threading
import time
import unicodedata


_MAX_LINE = 64 * 1024
_MAX_OUTPUT = 256 * 1024
_MAX_MODELS = 500


class CodexModelsError(RuntimeError):
    """Sanitized failure while querying the local Codex model list."""


def validated_model(value):
    """Return a bounded model ID; reject blank, control-containing, or long IDs."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 128:
        raise ValueError("Invalid model ID")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("Invalid model ID")
    value = value.strip()
    if not value:
        raise ValueError("Invalid model ID")
    return value


def _response(messages, request_id, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexModelsError("Codex app-server timed out.")
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise CodexModelsError("Codex app-server timed out.") from exc
        if message is None:
            raise CodexModelsError("Codex app-server exited before replying.")
        if message is _MALFORMED:
            raise CodexModelsError("Codex app-server returned malformed output.")
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise CodexModelsError("Codex app-server rejected the request (authentication or unsupported CLI).")
        if "result" not in message or not isinstance(message["result"], dict):
            raise CodexModelsError("Codex app-server returned malformed output.")
        return message["result"]


_MALFORMED = object()


def fetch_codex_models(executable="codex", timeout=8):
    """List locally available Codex models via app-server; never performs inference."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be positive")
    try:
        env = os.environ.copy()
        env.pop("SOCKET_STREAM_API_KEY", None)
        child = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise CodexModelsError("Codex CLI is unavailable or failed to start.") from exc

    messages = queue.Queue(maxsize=8)
    stopped = threading.Event()
    deadline = time.monotonic() + timeout

    def publish(item):
        while not stopped.is_set():
            try:
                messages.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def read_stdout():
        total = 0
        try:
            while not stopped.is_set():
                line = child.stdout.readline(_MAX_LINE + 1)
                if not line:
                    publish(None)
                    return
                total += len(line)
                if len(line) > _MAX_LINE or total > _MAX_OUTPUT:
                    publish(_MALFORMED)
                    return
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    publish(_MALFORMED)
                    return
                publish(message)
        except (OSError, ValueError):
            publish(None)

    reader = threading.Thread(target=read_stdout, name="codex-model-list", daemon=True)
    reader.start()

    def send(payload):
        if time.monotonic() >= deadline:
            raise CodexModelsError("Codex app-server timed out.")
        try:
            child.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            child.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as exc:
            raise CodexModelsError("Codex app-server transport failed.") from exc

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "socket_stream_local", "title": "SocketStream", "version": "1"}
        }})
        _response(messages, 1, deadline)
        send({"jsonrpc": "2.0", "method": "initialized"})
        listed = []
        cursor, seen = None, set()
        request_id = 2
        while True:
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            send({"jsonrpc": "2.0", "id": request_id, "method": "model/list", "params": params})
            result = _response(messages, request_id, deadline)
            # Official v2 ModelListResponse uses `data`; Model.model is the
            # inference ID, which need not equal its catalog entry's `id`.
            models = result.get("data")
            if not isinstance(models, list) or len(models) + len(listed) > _MAX_MODELS:
                raise CodexModelsError("Codex app-server returned an invalid model list.")
            for model in models:
                if not isinstance(model, dict):
                    raise CodexModelsError("Codex app-server returned an invalid model list.")
                try:
                    model_id = validated_model(model.get("model"))
                except ValueError as exc:
                    raise CodexModelsError("Codex app-server returned an invalid model list.") from exc
                try:
                    name = validated_model(model.get("displayName", model_id))
                except ValueError:
                    name = model_id
                listed.append({"id": model_id, "name": name})
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen or request_id >= 10:
                raise CodexModelsError("Codex app-server returned invalid pagination.")
            seen.add(cursor)
            request_id += 1
        return listed
    finally:
        stopped.set()
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except OSError:
                pass
            child.wait()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        reader.join(timeout=0.1)
