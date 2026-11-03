#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
# Let `context-cli overlay` find the floating overlay without a capture of its own.
CONTEXT_LENS_OVERLAY="$ROOT/apps/context-desktop/ui/floating_overlay.py"
export CONTEXT_LENS_OVERLAY
# Honor a user-set config path; never invent one. Global hotkeys are not installed here.
if [ -n "${CONTEXT_LENS_CONFIG:-}" ]; then export CONTEXT_LENS_CONFIG; fi
cargo build -p context-cli
exec python3 apps/context-desktop/ui/linux-gtk.py
