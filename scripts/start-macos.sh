#!/bin/sh
# Launch SocketStream on macOS.
#
# The installed app bundle is preferred because macOS TCC permissions
# (Accessibility, Screen Recording) are attached to the signed bundle rather
# than to a development binary. Build or refresh it with:
#
#   ./scripts/build-macos-app.sh
#
# Use --dev to compile the Rust CLI and Swift helpers and run the development
# GUI directly. That path is useful for UI work but may need its own permission
# grants and does not include the bundle layout.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

# Honor a user-set config path; never invent one.
if [ -n "${SOCKET_STREAM_CONFIG:-}" ]; then export SOCKET_STREAM_CONFIG; fi

MODE=installed
for argument in "$@"; do
  case "$argument" in
    --dev) MODE=dev ;;
    --installed) MODE=installed ;;
    -h|--help)
      echo "Usage: $0 [--dev|--installed]"
      exit 0
      ;;
    *)
      echo "unknown option: $argument" >&2
      exit 2
      ;;
  esac
done

if [ "$MODE" = "dev" ]; then
  cargo build -p socket_stream
  "$ROOT/scripts/build-macos-helpers.sh"
  exec "$ROOT/target/native-helpers/socket_stream_gui"
fi

BUNDLE="${SOCKET_STREAM_APP:-$HOME/Applications/SocketStream.app}"
if [ ! -d "$BUNDLE" ]; then
  BUNDLE="$ROOT/dist/SocketStream.app"
fi
if [ ! -d "$BUNDLE" ]; then
  echo "SocketStream.app was not found." >&2
  echo "Build it with: $ROOT/scripts/build-macos-app.sh" >&2
  echo "Or run the development GUI with: $0 --dev" >&2
  exit 1
fi

echo "Opening $BUNDLE"
exec open -n "$BUNDLE"
