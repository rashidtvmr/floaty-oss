#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
# Honor a user-set config path; never invent one. Global hotkeys are not installed here.
if [ -n "${CONTEXT_LENS_CONFIG:-}" ]; then export CONTEXT_LENS_CONFIG; fi
cargo build -p context-cli
"$ROOT/scripts/build-macos-helpers.sh"
exec "$ROOT/target/native-helpers/context-lens-gui"
