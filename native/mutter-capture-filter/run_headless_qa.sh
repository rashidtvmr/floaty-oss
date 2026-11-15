#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${1:?Usage: run_headless_qa.sh MUTTER_BUILD OUTPUT_DIR}"
OUTPUT="${2:?Specify a new directory for synthetic QA evidence}"
shift 2
meson devenv -C "$BUILD" dbus-run-session -- python3 "$HERE/qa_headless.py" "$BUILD" "$OUTPUT" "$@"
