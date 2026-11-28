#!/usr/bin/env bash
# Run from a clone with Rust, cargo-llvm-cov, llvm-tools-preview and coverage.py.
set -euo pipefail
SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$SCRIPT_ROOT"
BASE_REF=${1:-origin/main}
mkdir -p target/screen-coverage
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
# Keep instrumented binaries separate from normal developer builds. Every
# acceptance fixture must use this freshly built target, not a cached CLI.
export CARGO_TARGET_DIR="$SCRIPT_ROOT/target/screen-coverage/build"
export CARGO_LLVM_COV_TARGET_DIR="$CARGO_TARGET_DIR"
export CARGO_LLVM_COV_BUILD_DIR="$CARGO_TARGET_DIR"
# cargo-llvm-cov emits the instrumentation/profile environment for child
# processes too, so Python CLI acceptance contributes real Rust coverage.
eval "$(cargo llvm-cov show-env --sh)"
# The external-test workflow requires cleaning AFTER show-env; otherwise
# cached non-instrumented unit-test executables can be reused silently.
cargo llvm-cov clean --workspace
cargo test --workspace --locked --quiet
cargo build --workspace --locked
export SOCKET_STREAM_TEST_CLI="$CARGO_TARGET_DIR/debug/socket_stream"
test -x "$SOCKET_STREAM_TEST_CLI"
export COVERAGE_FILE="$SCRIPT_ROOT/target/screen-coverage/.coverage"
python3 -m coverage run --branch --source=scripts,apps/socket_stream_desktop/ui,native/mutter-capture-filter \
  -m unittest discover -s tests -q
python3 -m coverage run --append --branch --source=scripts,apps/socket_stream_desktop/ui,native/mutter-capture-filter \
  -m unittest discover -s tests/acceptance -q
python3 -m coverage json -o target/screen-coverage/python.json
cargo llvm-cov report --lcov --output-path target/screen-coverage/rust.lcov
python3 scripts/check_screen_coverage.py --base-ref "$BASE_REF"
