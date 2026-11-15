#!/bin/sh
# Downloads the public ggml tiny English model for local whisper.cpp transcription.
# The model is committed to Git; this script is the fallback for a missing local
# copy (fresh archives ship it, shallow checkouts may not). Audio never leaves
# the machine.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODEL="$ROOT/models/ggml-tiny.en.bin"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
mkdir -p "$ROOT/models"
if [ -s "$MODEL" ]; then
  echo "Existing local model: $MODEL"
  exit 0
fi
TEMP="$MODEL.part"
trap 'rm -f "$TEMP"' EXIT
curl --fail --location --retry 2 --connect-timeout 15 --output "$TEMP" "$URL"
SIZE=$(wc -c < "$TEMP" | tr -d ' ')
if [ "$SIZE" -lt 20000000 ]; then
  echo "Model download incomplete" >&2
  exit 1
fi
mv "$TEMP" "$MODEL"
echo "Local model saved: $MODEL"
