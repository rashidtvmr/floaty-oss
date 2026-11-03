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
build_if_needed "$ROOT/scripts/macos-accessibility.swift" "$OUT/context-lens-a11y"
build_if_needed "$ROOT/scripts/macos-ocr.swift" "$OUT/context-lens-ocr"
build_if_needed "$ROOT/apps/context-desktop/ui/mac-app.swift" "$OUT/context-lens-gui"
if [ ! -x "$OUT/context-lens-system-audio" ] || [ "$ROOT/scripts/macos-system-audio.swift" -nt "$OUT/context-lens-system-audio" ]; then
  echo "Compiling macos-system-audio.swift"
  swiftc -parse-as-library -O -framework AVFoundation -framework ScreenCaptureKit "$ROOT/scripts/macos-system-audio.swift" -o "$OUT/context-lens-system-audio"
fi
echo "Native macOS helpers ready."
