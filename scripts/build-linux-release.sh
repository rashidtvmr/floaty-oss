#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -eq 0 ]; then ARCH=x86_64; else ARCH=$1; fi
case "$ARCH" in x86_64|aarch64) ;; *) echo "Usage: $0 [x86_64|aarch64]" >&2; exit 2;; esac
TARGET="$ARCH-unknown-linux-musl"
BINARY="$ROOT/target/$TARGET/release/socket_stream"
# Always compile the checked-out source before packaging. A failed build must
# never silently ship an old ELF left in target/ by a previous release.
(cd "$ROOT" && cargo zigbuild --locked --release -p socket_stream --target "$TARGET")
test -x "$BINARY" || { echo "Missing ELF: cargo zigbuild -p socket_stream --release --target $TARGET" >&2; exit 1; }
file "$BINARY" | grep -q 'ELF 64-bit' || { echo "Expected Linux ELF" >&2; exit 1; }
SPEECH=${SOCKET_STREAM_MOONSHINE_DIR:-"$ROOT/target/native-helpers/moonshine/socket_stream_moonshine"}
python3 - "$SPEECH/PLATFORM.json" "$ARCH" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    assert data['system'] == 'Linux' and data['machine'] == sys.argv[2]
except (OSError, ValueError, KeyError, AssertionError):
    raise SystemExit('Build scripts/build-moonshine.py on the target Linux architecture, then set SOCKET_STREAM_MOONSHINE_DIR to its socket_stream_moonshine directory.')
PY
NAME="socket_stream_linux-$ARCH-$(date +%Y%m%d)"
DIST_DIR=${SOCKET_STREAM_DIST_DIR:-"$ROOT/dist"}
mkdir -p "$DIST_DIR"
STAGING=$(mktemp -d "$DIST_DIR/.staging.XXXXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
PKG="$STAGING/$NAME"
mkdir -p "$PKG/bin" "$PKG/apps/socket_stream_desktop/ui" "$PKG/scripts" "$PKG/models" "$PKG/examples" "$PKG/tests/fixtures/live_practice" "$PKG/tests/fixtures/mock_provider" "$PKG/tests/acceptance" "$PKG/security-lab"
cp "$BINARY" "$PKG/bin/socket_stream"
mkdir -p "$PKG/target/native-helpers/moonshine"
cp -R "$SPEECH" "$PKG/target/native-helpers/moonshine/socket_stream_moonshine"
# Ship every Python module in the UI directory so a new helper module can
# never be missed by packaging again (mac-app.swift is macOS-only and stays
# out of the Linux archive).
for ui_module in "$ROOT"/apps/socket_stream_desktop/ui/*.py; do
  cp "$ui_module" "$PKG/apps/socket_stream_desktop/ui/$(basename "$ui_module")"
done
cp "$ROOT/security-lab/overlay_probe.py" "$PKG/security-lab/overlay_probe.py"
cp "$ROOT/security-lab/README.md" "$PKG/security-lab/README.md"
for page in screen-share-test.html screen-share-test.js capture-verification.js; do
  cp "$ROOT/security-lab/$page" "$PKG/security-lab/$page"
done
cp "$ROOT/scripts/linux-accessibility.py" "$PKG/scripts/linux-accessibility.py"
cp "$ROOT/scripts/linux-portal-capture.py" "$PKG/scripts/linux-portal-capture.py"
cp "$ROOT/scripts/linux-check-dependencies.sh" "$PKG/check-dependencies.sh"
cp "$ROOT/scripts/linux-install-deps-debian.sh" "$PKG/install-dependencies-debian.sh"
cp "$ROOT/scripts/linux-portable-launch.sh" "$PKG/socket_stream"
for optional in mock_provider_server.py soak_replay.py; do
    if [ -f "$ROOT/scripts/$optional" ]; then
        cp "$ROOT/scripts/$optional" "$PKG/scripts/$optional"
    fi
done
cp "$ROOT/scripts/setup-whisper-model.sh" "$PKG/scripts/setup-whisper-model.sh"
cp "$ROOT/examples/sample-question.txt" "$PKG/examples/sample-question.txt"
cp "$ROOT/tests/fixtures/sample-code.png" "$PKG/tests/fixtures/sample-code.png"
cp "$ROOT/tests/fixtures/live_practice/events.jsonl" "$PKG/tests/fixtures/live_practice/events.jsonl"
cp "$ROOT/tests/fixtures/mock_provider/events.jsonl" "$PKG/tests/fixtures/mock_provider/events.jsonl"
cp "$ROOT/tests/fixtures/mock_provider/questions.txt" "$PKG/tests/fixtures/mock_provider/questions.txt"
cp "$ROOT/tests/test_shortcuts.py" "$PKG/tests/test_shortcuts.py"
for gtk_check in gtk_workspace_checks.py gtk_input.py run_gtk_input_checks.py linux_explicit_window_checks.py; do
    cp "$ROOT/tests/$gtk_check" "$PKG/tests/$gtk_check"
done
for optional in test_a11y_helpers.py test_mock_provider.py test_soak_replay.py; do
    if [ -f "$ROOT/tests/$optional" ]; then
        cp "$ROOT/tests/$optional" "$PKG/tests/$optional"
    fi
done
if [ -f "$ROOT/tests/acceptance/test_provider_e2e.py" ]; then
    cp "$ROOT/tests/acceptance/test_provider_e2e.py" "$PKG/tests/acceptance/test_provider_e2e.py"
fi
cp "$ROOT/tests/acceptance/test_config_persistence.py" "$PKG/tests/acceptance/test_config_persistence.py"
cp "$ROOT/tests/acceptance/test_manual_vision_e2e.py" "$PKG/tests/acceptance/test_manual_vision_e2e.py"
cp "$ROOT/tests/acceptance/test_speech_turns_e2e.py" "$PKG/tests/acceptance/test_speech_turns_e2e.py"
cp "$ROOT/tests/acceptance/test_provider_profiles_e2e.py" "$PKG/tests/acceptance/test_provider_profiles_e2e.py"
cp "$ROOT/docs/LINUX-TESTING.md" "$PKG/README-LINUX.md"
cp "$ROOT/docs/AUTH-AND-ENDPOINTS.md" "$PKG/AUTH-AND-ENDPOINTS.md"
cp "$ROOT/docs/STEALTH-OVERLAY.md" "$PKG/STEALTH-OVERLAY.md"
cp "$ROOT/docs/GNOME-CAPTURE-EXCLUSION.md" "$PKG/GNOME-CAPTURE-EXCLUSION.md"
cp "$ROOT/docs/MOONSHINE.md" "$PKG/MOONSHINE.md"
cp "$ROOT/docs/READABLE-ANSWER-UI.md" "$PKG/READABLE-ANSWER-UI.md"
cp "$ROOT/docs/PANEL-SESSION-PROFILES.md" "$PKG/PANEL-SESSION-PROFILES.md"
if [ -f "$ROOT/docs/SHORTCUTS.md" ]; then
    cp "$ROOT/docs/SHORTCUTS.md" "$PKG/SHORTCUTS.md"
fi
cp "$ROOT/docs/A11Y-TREE-SHORTCUTS.md" "$PKG/A11Y-TREE-SHORTCUTS.md"
cp "$ROOT/docs/EXTRACTION-BENCHMARK.md" "$PKG/EXTRACTION-BENCHMARK.md"
cp "$ROOT/docs/AI-PROMPT-CACHING-AND-TURNS.md" "$PKG/AI-PROMPT-CACHING-AND-TURNS.md"
cp "$ROOT/crates/socket_stream_core/prompts/practice-system-v3.md" "$PKG/AI-SYSTEM-PROMPT.md"
test ! -f "$ROOT/models/ggml-tiny.en.bin" || cp "$ROOT/models/ggml-tiny.en.bin" "$PKG/models/ggml-tiny.en.bin"
for f in "$ROOT/LICENSE" "$ROOT/COPYING"; do if [ -f "$f" ]; then cp "$f" "$PKG/"; break; fi; done
if ! git -C "$ROOT" rev-parse --verify HEAD > "$PKG/SOURCE-COMMIT.txt" 2>/dev/null; then
    printf 'uncommitted source tree (no Git commit available)\n' > "$PKG/SOURCE-COMMIT.txt"
fi
chmod 755 "$PKG/socket_stream" "$PKG/bin/socket_stream" "$PKG/check-dependencies.sh" "$PKG/install-dependencies-debian.sh"
OUT="$DIST_DIR/$NAME.tar.gz"
tar -C "$STAGING" -czf "$OUT" "$NAME"
(cd "$DIST_DIR" && shasum -a 256 "$NAME.tar.gz") > "$OUT.sha256"
echo "Linux archive: $OUT"
echo "Size: $(du -h "$OUT" | cut -f1)"
cat "$OUT.sha256"
