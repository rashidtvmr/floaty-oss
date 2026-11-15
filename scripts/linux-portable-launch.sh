#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export SOCKET_STREAM_HOME="$ROOT"
export SOCKET_STREAM_CLI="$ROOT/bin/socket_stream"
test -x "$SOCKET_STREAM_CLI" || { echo "Rust binary missing; re-extract Linux release" >&2; exit 2; }
if [ "$#" -gt 0 ]; then exec "$SOCKET_STREAM_CLI" "$@"; fi
if [ -z "$(printenv DISPLAY 2>/dev/null || true)$(printenv WAYLAND_DISPLAY 2>/dev/null || true)" ]; then
  echo "No graphical session. Run ./socket_stream doctor" >&2
  exit 2
fi
if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 "$ROOT/apps/socket_stream_desktop/ui/linux-gtk.py"; fi
exec python3 "$ROOT/apps/socket_stream_desktop/ui/linux-gtk.py"
