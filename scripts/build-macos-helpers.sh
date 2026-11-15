#!/bin/sh
# Compile small AppKit / Vision / Accessibility native helpers for fast repeated use.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$ROOT/target/native-helpers"
mkdir -p "$OUT"
build_if_needed() {
  SOURCE="$1"
  TARGET="$2"
  if [ ! -x "$TARGET" ] || [ "$SOURCE" -nt "$TARGET" ]; then
    echo "Compiling $(basename "$SOURCE")"
    swiftc -O -framework AppKit -framework ApplicationServices -framework Vision "$SOURCE" -o "$TARGET"
  fi
}
if [ ! -x "$OUT/socket_stream_a11y" ] || [ "$ROOT/scripts/macos-accessibility.swift" -nt "$OUT/socket_stream_a11y" ]; then
  echo "Compiling macos-accessibility.swift"
  swiftc -D SOCKET_STREAM_ACCESSIBILITY_STANDALONE -O -framework AppKit -framework ApplicationServices \
    "$ROOT/scripts/macos-accessibility.swift" -o "$OUT/socket_stream_a11y"
fi
build_if_needed "$ROOT/scripts/macos-ocr.swift" "$OUT/socket_stream_ocr"
if [ ! -x "$OUT/socket_stream_gui" ] || \
   [ "$ROOT/apps/socket_stream_desktop/ui/mac-app.swift" -nt "$OUT/socket_stream_gui" ] || \
   [ "$ROOT/scripts/macos-accessibility.swift" -nt "$OUT/socket_stream_gui" ]; then
  echo "Compiling mac-app.swift (with in-process AX reader)"
  cat "$ROOT/apps/socket_stream_desktop/ui/mac-app.swift" "$ROOT/scripts/macos-accessibility.swift" > "$OUT/main.swift"
  sed -i '' 's/^#if SOCKET_STREAM_WITH_AX_READER$/#if true/' "$OUT/main.swift"
  swiftc -D SOCKET_STREAM_WITH_AX_READER -O -framework AppKit -framework ApplicationServices -framework Vision \
    "$OUT/main.swift" -o "$OUT/socket_stream_gui"
fi
if [ ! -x "$OUT/socket_stream_system_audio" ] || [ "$ROOT/scripts/macos-system-audio.swift" -nt "$OUT/socket_stream_system_audio" ]; then
  echo "Compiling macos-system-audio.swift"
  swiftc -parse-as-library -O -framework AVFoundation -framework ScreenCaptureKit "$ROOT/scripts/macos-system-audio.swift" -o "$OUT/socket_stream_system_audio"
fi
if [ ! -x "$OUT/socket_stream_audio_stream" ] || [ "$ROOT/scripts/macos-audio-stream.swift" -nt "$OUT/socket_stream_audio_stream" ] || [ "$ROOT/scripts/macos-audio-stream.plist" -nt "$OUT/socket_stream_audio_stream" ]; then
  echo "Compiling macos-audio-stream.swift"
  swiftc -parse-as-library -O -framework AVFoundation -framework ScreenCaptureKit \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$ROOT/scripts/macos-audio-stream.plist" \
    "$ROOT/scripts/macos-audio-stream.swift" -o "$OUT/socket_stream_audio_stream"
fi
echo "Native macOS helpers ready."
