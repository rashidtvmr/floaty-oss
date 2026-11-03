#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Default behaviour is unchanged: serve the browser capture lab.
# `probe` runs the GTK stealth-overlay probe instead.
case "${1:-}" in
    probe)
        shift
        exec python3 "$ROOT/overlay_probe.py" "$@"
        ;;
    *)
        exec python3 "$ROOT/serve.py" "$@"
        ;;
esac
