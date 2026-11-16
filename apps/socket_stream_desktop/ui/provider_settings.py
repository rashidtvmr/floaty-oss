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
import fcntl
import secrets
import stat
from pathlib import Path

PROVIDER_SETTING_KEYS = (
    "SOCKET_STREAM_BASE_URL",
    "SOCKET_STREAM_MODEL",
    "SOCKET_STREAM_API_KEY",
    "SOCKET_STREAM_OLLAMA_URL",
    "SOCKET_STREAM_OLLAMA_MODEL",
    "SOCKET_STREAM_CODEX_MODEL",
    "SOCKET_STREAM_PROVIDER",
)

_LIMIT = 256 * 1024
_PROVIDERS = ("mock", "codex", "openai-compatible", "ollama")


def _validate_metadata(data):
    if any(key in data and not isinstance(data[key], str) for key in PROVIDER_SETTING_KEYS):
        raise ValueError("Invalid legacy provider setting")
    for key in ("active_profile", "fallback_profile"):
        if key in data and not isinstance(data[key], str):
            raise ValueError("Invalid saved profile selector")
    if "profiles" not in data:
        if data.get("active_profile") or data.get("fallback_profile"):
            raise ValueError("Missing saved profile book")
        return
    profiles = data["profiles"]
    if not isinstance(profiles, dict) or len(profiles) > 32:
        raise ValueError("Invalid saved profile book")
    for name, item in profiles.items():
        if (not isinstance(name, str) or not name.strip() or len(name) > 64
                or not name.isprintable() or not isinstance(item, dict)
                or item.get("provider") not in _PROVIDERS or not isinstance(item.get("settings"), dict)):
            raise ValueError("Invalid saved profile")
        if any(key in item["settings"] and not isinstance(item["settings"][key], str) for key in PROVIDER_SETTING_KEYS):
            raise ValueError("Invalid saved profile setting")
    for key in ("active_profile", "fallback_profile"):
        value = data.get(key, "")
        if not isinstance(value, str) or (value and value not in profiles):
            raise ValueError("Invalid saved profile selector")
    if data.get("fallback_profile") and data.get("fallback_profile") == data.get("active_profile"):
        raise ValueError("Primary and fallback profiles must differ")


def _directory(create=False):
    path = provider_settings_path()
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ValueError("Unsafe provider settings directory")
    return fd


def _lock(directory):
    fd = os.open(".provider-settings.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe provider settings lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(directory, name, strict=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _LIMIT):
            raise ValueError("Unsafe provider settings file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            text = handle.read(_LIMIT + 1)
        if len(text.encode("utf-8")) > _LIMIT:
            raise ValueError("Provider settings too large")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Invalid provider settings")
        _validate_metadata(data)
        return data
    except (ValueError, UnicodeError, RecursionError):
        if strict:
            raise ValueError("Invalid provider settings; existing file preserved") from None
        return {}
    finally:
        os.close(fd)


def _load():
    path = provider_settings_path()
    try:
        directory = _directory()
        try:
            return _read(directory, path.name)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        return {}


def _update(change):
    """Private from creation, bounded, locked and directory-relative replacement."""
    path = provider_settings_path()
    directory = _directory(create=True)
    lock = None
    temporary = None
    try:
        lock = _lock(directory)
        payload = change(_read(directory, path.name, strict=True))
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _LIMIT:
            raise ValueError("Provider settings too large")
        temporary = ".provider-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
        return path
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _settings(env):
    return {key: str(env.get(key, "")) for key in PROVIDER_SETTING_KEYS}


def _book(data):
    _validate_metadata(data)
    profiles = data.get("profiles", {})
    profiles = {name: item for name, item in profiles.items()
                if isinstance(name, str) and isinstance(item, dict)
                and item.get("provider") in _PROVIDERS and isinstance(item.get("settings"), dict)} if isinstance(profiles, dict) else {}
    if not profiles and any(key in data for key in PROVIDER_SETTING_KEYS):
        selected = data.get("SOCKET_STREAM_PROVIDER")
        profiles = {"Default": {"provider": selected if selected in _PROVIDERS else "openai-compatible", "settings": _settings(data)}}
    active = data.get("active_profile", "")
    if active not in profiles:
        active = next(iter(profiles), "")
    fallback = data.get("fallback_profile", "")
    return {"profiles": profiles, "active_profile": active,
            "fallback_profile": fallback if fallback in profiles and fallback != active else ""}


def load_provider_profiles():
    return _book(_load())


def save_provider_profile(name, provider, env, fallback=""):
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or not name.isprintable():
        raise ValueError("Profile name must be 1–64 printable characters")
    if provider not in _PROVIDERS:
        raise ValueError("Invalid provider")
    name = name.strip()

    def change(data):
        book = _book(data)
        if fallback and (fallback not in book["profiles"] or fallback == name):
            raise ValueError("Choose a different saved fallback profile")
        item = dict(book["profiles"].get(name, {}))
        values = dict(item.get("settings", {}))
        values.update(_settings(env))
        item.update(provider=provider, settings=values)
        book["profiles"][name] = item
        if len(book["profiles"]) > 32:
            raise ValueError("At most 32 provider profiles are supported")
        book.update(active_profile=name, fallback_profile=fallback)
        data.update(_settings(env), **book, version=2, SOCKET_STREAM_PROVIDER=provider)
        return data

    return _update(change)


def provider_settings_path() -> Path:
    """Path of the saved provider settings file (user-only permissions)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(config_home) / "socket_stream" / "provider-settings.json"


def load_provider_settings() -> dict:
    """Load saved provider settings; missing or malformed files return {}."""
    data = _load()
    book = _book(data)
    if book["active_profile"]:
        profile = book["profiles"][book["active_profile"]]
        data = dict(profile["settings"], SOCKET_STREAM_PROVIDER=profile["provider"])
    return {key: str(data[key]) for key in PROVIDER_SETTING_KEYS if key in data}


def save_provider_settings(env: dict) -> Path:
    """Persist provider settings with 0600 permissions; returns the path."""
    def change(data):
        values = _settings(env)
        data.update(values)
        if data.get("active_profile") in data.get("profiles", {}):
            item = data["profiles"][data["active_profile"]]
            item["settings"] = values
            if env.get("SOCKET_STREAM_PROVIDER") in _PROVIDERS:
                item["provider"] = env["SOCKET_STREAM_PROVIDER"]
        return data
    return _update(change)


def clear_provider_settings() -> bool:
    """Delete saved provider settings; safe when nothing is saved."""
    try:
        directory = _directory()
    except FileNotFoundError:
        return False
    lock = None
    try:
        lock = _lock(directory)
        path = provider_settings_path()
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe provider settings file")
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)
