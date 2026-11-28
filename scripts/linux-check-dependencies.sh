#!/bin/sh
set -u
UI_PYTHON=
for argument in "$@"; do
  case "$argument" in
    --help)
      echo "Usage: $0 [--ui-python=/absolute/path/to/python3]"
      echo "Checks the selected GTK runtime and the distro Python used by native capture helpers separately."
      exit 0 ;;
    --ui-python=*)
      choice=${argument#--ui-python=}
      case "$choice" in
        /*) ;;
        *) echo "--ui-python requires an absolute executable path." >&2; exit 2 ;;
      esac
      if [ ! -f "$choice" ] || [ ! -x "$choice" ]; then
        echo "--ui-python requires an executable file." >&2; exit 2
      fi
      if [ -n "$UI_PYTHON" ] && [ "$UI_PYTHON" != "$choice" ]; then
        echo "Choose exactly one UI Python interpreter." >&2; exit 2
      fi
      # Keep a venv's invocation path intact; resolving its symlink would
      # select the base interpreter and lose the private GUI packages.
      UI_PYTHON=$choice ;;
    *) echo "Usage: $0 [--ui-python=/absolute/path/to/python3]" >&2; exit 2 ;;
  esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# The release copies this script to its root; a checkout keeps it in scripts/.
if [ -f "$ROOT/../Cargo.toml" ] && [ -f "$ROOT/../apps/socket_stream_desktop/ui/linux-gtk.py" ]; then
  ROOT=$(CDPATH= cd -- "$ROOT/.." && pwd)
fi
echo "SocketStream Linux dependency check ($(uname -s)/$(uname -m))"
missing=0
for tool in tesseract ffmpeg pactl gdbus timeout; do
  if command -v "$tool" >/dev/null 2>&1; then echo "[OK] $tool"
  else echo "[MISSING] $tool"; missing=1; fi
done
NATIVE_PYTHON=
if [ -x /usr/bin/python3 ]; then NATIVE_PYTHON=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then NATIVE_PYTHON=$(command -v python3); fi
if [ -z "$UI_PYTHON" ]; then
  UI_PYTHON=$NATIVE_PYTHON
  set --
else
  # Match the private-runtime launcher without changing the default source
  # launcher's Python behavior or the native helper interpreter.
  set -- -I -B
fi
if [ -n "$UI_PYTHON" ]; then
  printf '[INFO] UI Python: %s\n' "$UI_PYTHON"
  if ! timeout --kill-after=1s 5s "$UI_PYTHON" "$@" - 2>/dev/null <<'PY'
import gi
# Pycairo alone cannot marshal GTK draw contexts or input regions. Debian and
# Ubuntu ship that GI bridge separately, so exercise it before reporting ready.
try:
    import cairo
    gi.require_foreign("cairo")
except ImportError:
    print("[MISSING] GTK Cairo bridge; install python3-cairo and python3-gi-cairo.")
    raise SystemExit(1)
print("[OK] Pycairo and GTK Cairo bridge")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
print("[OK] GTK3 (UI Python)")
PY
  then echo "[MISSING] UI Python GTK/Cairo probe failed or timed out; check the selected runtime."; missing=1; fi
else
  echo "[MISSING] UI Python interpreter."
  missing=1
fi
if [ -n "$NATIVE_PYTHON" ]; then
  printf '[INFO] Native helper Python: %s\n' "$NATIVE_PYTHON"
  if timeout --kill-after=1s 5s "$NATIVE_PYTHON" - 2>/dev/null <<'PY'
import dbus, gi
gi.require_version("Atspi", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Atspi, Gst
print("[OK] DBus, AT-SPI2, GStreamer (native helper Python)")
PY
  then :
  else
    echo "[MISSING] Native helper Python DBus/AT-SPI2/GStreamer probe failed or timed out; check distro GI and D-Bus packages."
    missing=1
  fi
else
  echo "[MISSING] Native helper Python interpreter."
  missing=1
fi
if command -v gst-inspect-1.0 >/dev/null 2>&1; then
  for name in pipewiresrc videoconvert videorate pngenc appsink; do
    if timeout --kill-after=1s 5s gst-inspect-1.0 "$name" >/dev/null 2>&1; then echo "[OK] GStreamer $name"
    else echo "[MISSING] GStreamer $name"; missing=1; fi
  done
else echo "[MISSING] gst-inspect-1.0"; missing=1; fi
if command -v ffmpeg >/dev/null 2>&1; then
  if devices=$(timeout --kill-after=1s 5s ffmpeg -hide_banner -devices 2>/dev/null) &&
     printf '%s\n' "$devices" | awk '$1 ~ /D/ && $2 == "pulse" {found=1} END {exit !found}'; then
    echo "[OK] FFmpeg PulseAudio input"
  else echo "[MISSING] FFmpeg PulseAudio input; install the distro ffmpeg package or repair its libraries."; missing=1; fi
fi
if command -v pactl >/dev/null 2>&1; then
  if timeout --kill-after=1s 5s pactl info >/dev/null 2>&1; then
    echo "[OK] PulseAudio-compatible audio server (read-only connection)"
  else echo "[MISSING] PulseAudio-compatible audio server; check the existing desktop PipeWire/PulseAudio session."; missing=1; fi
fi
if command -v tesseract >/dev/null 2>&1; then
  if languages=$(timeout --kill-after=1s 5s tesseract --list-langs 2>/dev/null) &&
     printf '%s\n' "$languages" | awk '$0 == "eng" {found=1} END {exit !found}'; then
    echo "[OK] Tesseract English model"
  else echo "[MISSING] Tesseract English model; install tesseract-ocr-eng or check TESSDATA_PREFIX."; missing=1; fi
fi
speech="$ROOT/target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
if [ ! -x "$speech" ]; then
  echo "[MISSING] Bundled Moonshine engine/model; reinstall the release or build scripts/build-moonshine.py in a source checkout."
  missing=1
elif [ -z "$NATIVE_PYTHON" ]; then
  echo "[MISSING] Python is required to validate Moonshine readiness."
  missing=1
elif readiness=$(timeout --kill-after=1s 20s "$speech" --check 2>/dev/null) &&
     printf '%s\n' "$readiness" | "$NATIVE_PYTHON" -c 'import json,sys; events=[json.loads(line) for line in sys.stdin if line.strip()]; sys.exit(0 if any(isinstance(e,dict) and e.get("type")=="ready" for e in events) else 1)' >/dev/null 2>&1; then
  echo "[OK] bundled Moonshine engine/model (--check; no audio device opened)"
else
  echo "[MISSING] Bundled Moonshine did not become ready; check its platform libraries/model or rebuild it."
  missing=1
fi
for tool in whisper-cli ollama; do
  if command -v "$tool" >/dev/null 2>&1; then echo "[OK] optional $tool"
  else echo "[OPTIONAL] $tool not installed"; fi
done
test ! -f "$ROOT/models/ggml-tiny.en.bin" || echo "[OK] bundled Whisper model"
echo "Desktop accessibility and portal approval still require checks in the logged-in session."
exit "$missing"
