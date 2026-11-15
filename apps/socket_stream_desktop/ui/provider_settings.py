"""Persistence for AI provider settings entered in the GTK settings dialog.

Pure standard library: importable with no GTK. The GTK layer
(``linux-gtk.py``) loads these values into its in-memory environment at
startup and saves them when the user applies the dialog.

The file is written with user-only permissions (0600) because it holds the
API key in plaintext — the same trade-off as most CLI credential files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROVIDER_SETTING_KEYS = (
    "SOCKET_STREAM_BASE_URL",
    "SOCKET_STREAM_MODEL",
    "SOCKET_STREAM_API_KEY",
    "SOCKET_STREAM_OLLAMA_URL",
    "SOCKET_STREAM_OLLAMA_MODEL",
)


def provider_settings_path() -> Path:
    """Path of the saved provider settings file (user-only permissions)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(config_home) / "socket_stream" / "provider-settings.json"


def load_provider_settings() -> dict:
    """Load saved provider settings; missing or malformed files return {}."""
    try:
        with open(provider_settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data[key]) for key in PROVIDER_SETTING_KEYS if key in data}


def save_provider_settings(env: dict) -> Path:
    """Persist provider settings with 0600 permissions; returns the path."""
    path = provider_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: env.get(key, "") for key in PROVIDER_SETTING_KEYS}
    temp = path.with_suffix(".json.part")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.chmod(temp, 0o600)
    temp.replace(path)
    return path


def clear_provider_settings() -> bool:
    """Delete saved provider settings; safe when nothing is saved."""
    try:
        os.unlink(provider_settings_path())
        return True
    except OSError:
        return False
