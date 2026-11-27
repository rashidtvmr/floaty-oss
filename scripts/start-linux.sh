#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
# Let `socket_stream overlay` find the floating overlay without a capture of its own.
SOCKET_STREAM_OVERLAY="$ROOT/apps/socket_stream_desktop/ui/floating_overlay.py"
export SOCKET_STREAM_OVERLAY
# Honor a user-set config path; never invent one. Global hotkeys are not installed here.
if [ -n "${SOCKET_STREAM_CONFIG:-}" ]; then export SOCKET_STREAM_CONFIG; fi
cargo build -p socket_stream
if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 apps/socket_stream_desktop/ui/linux-gtk.py; fi
exec python3 apps/socket_stream_desktop/ui/linux-gtk.py
