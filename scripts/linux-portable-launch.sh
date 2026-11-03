#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export CONTEXT_LENS_HOME="$ROOT"
export CONTEXT_LENS_CLI="$ROOT/bin/context-cli"
test -x "$CONTEXT_LENS_CLI" || { echo "Rust binary missing; re-extract Linux release" >&2; exit 2; }
if [ "$#" -gt 0 ]; then exec "$CONTEXT_LENS_CLI" "$@"; fi
if [ -z "$(printenv DISPLAY 2>/dev/null || true)$(printenv WAYLAND_DISPLAY 2>/dev/null || true)" ]; then
  echo "No graphical session. Run ./contextlens doctor" >&2
  exit 2
fi
if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 "$ROOT/apps/context-desktop/ui/linux-gtk.py"; fi
exec python3 "$ROOT/apps/context-desktop/ui/linux-gtk.py"
