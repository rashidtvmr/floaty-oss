"""List Pulse/PipeWire sources without opening a recording stream."""
import json

from source_preflight import _probe


def list_audio_devices(source, *, run=_probe):
    reply = run(["pactl", "-f", "json", "list", "sources"], timeout=3)
    if reply.returncode:
        raise ValueError("Audio device listing is unavailable")
    rows = json.loads(reply.stdout)
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("Invalid audio device list")
    result = [{"id": "default", "label": "Default playback monitor" if source == "system" else "Default microphone"}]
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 1024 or not name.isprintable():
            continue
        monitor = name.endswith(".monitor") or item.get("monitor_of_sink") not in (None, "n/a", 4294967295)
        if monitor != (source == "system"):
            continue
        description = str(item.get("description") or name)[:120]
        result.append({"id": name, "label": description})
    return result
