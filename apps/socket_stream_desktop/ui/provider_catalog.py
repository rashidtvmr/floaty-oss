"""Bounded model discovery with sanitized errors; listing never proves inference."""
import os
import subprocess

from codex_models import CodexModelsError, fetch_codex_models, validated_model
from source_preflight import _probe


def fetch_models(provider, cli, settings, *, run=_probe):
    if provider == "codex":
        return fetch_codex_models()
    if provider == "mock":
        return []
    if provider not in ("ollama", "openai-compatible"):
        raise ValueError("Unsupported provider")
    env = dict(os.environ)
    env.update(settings)
    env.pop("SOCKET_STREAM_FALLBACK_CONFIG", None)
    try:
        reply = run([str(cli), "provider-models", "--provider", provider], env=env, timeout=12)
        if reply.returncode:
            raise CodexModelsError("Model discovery failed. Check the endpoint, credentials and connectivity.")
        lines = reply.stdout.splitlines()
        if not lines or not lines[0].startswith("Connection and model-list request succeeded;"):
            raise ValueError("Unexpected model catalog")
        result = []
        for line in lines[1:]:
            if line == "The provider returned no models.":
                continue
            model = validated_model(line)
            if model not in {item["id"] for item in result}:
                result.append({"id": model, "name": model})
            if len(result) > 500:
                raise ValueError("Too many models")
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise CodexModelsError("Model discovery timed out or returned an invalid catalog. You can enter a model ID manually.") from exc
