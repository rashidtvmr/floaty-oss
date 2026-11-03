#!/bin/sh
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
echo "ContextLens Linux dependency check ($(uname -s)/$(uname -m))"
missing=0
for tool in python3 tesseract ffmpeg pactl; do
  if command -v "$tool" >/dev/null 2>&1; then echo "[OK] $tool"
  else echo "[MISSING] $tool"; missing=1; fi
done
if [ -x /usr/bin/python3 ]; then
  if ! /usr/bin/python3 - <<'PY'
import dbus, gi
gi.require_version("Gtk", "3.0")
gi.require_version("Atspi", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Atspi, Gst
print("[OK] DBus, GTK3, AT-SPI2, GStreamer")
PY
  then missing=1; fi
fi
if command -v gst-inspect-1.0 >/dev/null 2>&1; then
  for name in pipewiresrc videoconvert videorate pngenc appsink; do
    if gst-inspect-1.0 "$name" >/dev/null 2>&1; then echo "[OK] GStreamer $name"
    else echo "[MISSING] GStreamer $name"; missing=1; fi
  done
else echo "[MISSING] gst-inspect-1.0"; missing=1; fi
for tool in whisper-cli ollama; do
  if command -v "$tool" >/dev/null 2>&1; then echo "[OK] optional $tool"
  else echo "[OPTIONAL] $tool not installed"; fi
done
test ! -f "$ROOT/models/ggml-tiny.en.bin" || echo "[OK] bundled Whisper model"
exit "$missing"
