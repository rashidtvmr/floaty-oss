#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -eq 0 ]; then ARCH=x86_64; else ARCH=$1; fi
case "$ARCH" in x86_64|aarch64) ;; *) echo "Usage: $0 [x86_64|aarch64]" >&2; exit 2;; esac
TARGET="$ARCH-unknown-linux-musl"
BINARY="$ROOT/target/$TARGET/release/context-cli"
test -x "$BINARY" || { echo "Missing ELF: cargo zigbuild -p context-cli --release --target $TARGET" >&2; exit 1; }
file "$BINARY" | grep -q 'ELF 64-bit' || { echo "Expected Linux ELF" >&2; exit 1; }
NAME="contextlens-linux-$ARCH-$(date +%Y%m%d)"
mkdir -p "$ROOT/dist"
STAGING=$(mktemp -d "$ROOT/dist/.staging.XXXXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
PKG="$STAGING/$NAME"
mkdir -p "$PKG/bin" "$PKG/apps/context-desktop/ui" "$PKG/scripts" "$PKG/models" "$PKG/examples" "$PKG/tests/fixtures/live_practice" "$PKG/tests/acceptance" "$PKG/security-lab"
cp "$BINARY" "$PKG/bin/context-cli"
cp "$ROOT/apps/context-desktop/ui/linux-gtk.py" "$PKG/apps/context-desktop/ui/linux-gtk.py"
cp "$ROOT/apps/context-desktop/ui/live_state.py" "$PKG/apps/context-desktop/ui/live_state.py"
cp "$ROOT/apps/context-desktop/ui/overlay_guard.py" "$PKG/apps/context-desktop/ui/overlay_guard.py"
cp "$ROOT/apps/context-desktop/ui/floating_overlay.py" "$PKG/apps/context-desktop/ui/floating_overlay.py"
cp "$ROOT/apps/context-desktop/ui/shortcuts.py" "$PKG/apps/context-desktop/ui/shortcuts.py"
cp "$ROOT/security-lab/overlay_probe.py" "$PKG/security-lab/overlay_probe.py"
cp "$ROOT/security-lab/README.md" "$PKG/security-lab/README.md"
cp "$ROOT/scripts/linux-accessibility.py" "$PKG/scripts/linux-accessibility.py"
cp "$ROOT/scripts/linux-portal-capture.py" "$PKG/scripts/linux-portal-capture.py"
cp "$ROOT/scripts/linux-check-dependencies.sh" "$PKG/check-dependencies.sh"
cp "$ROOT/scripts/linux-install-deps-debian.sh" "$PKG/install-dependencies-debian.sh"
cp "$ROOT/scripts/linux-portable-launch.sh" "$PKG/contextlens"
cp "$ROOT/scripts/mock_provider_server.py" "$PKG/scripts/mock_provider_server.py"
cp "$ROOT/scripts/soak_replay.py" "$PKG/scripts/soak_replay.py"
cp "$ROOT/scripts/setup-whisper-model.sh" "$PKG/scripts/setup-whisper-model.sh"
cp "$ROOT/examples/sample-question.txt" "$PKG/examples/sample-question.txt"
cp "$ROOT/tests/fixtures/sample-code.png" "$PKG/tests/fixtures/sample-code.png"
cp "$ROOT/tests/fixtures/live_practice/events.jsonl" "$PKG/tests/fixtures/live_practice/events.jsonl"
cp "$ROOT/tests/test_a11y_helpers.py" "$PKG/tests/test_a11y_helpers.py"
cp "$ROOT/tests/test_shortcuts.py" "$PKG/tests/test_shortcuts.py"
cp "$ROOT/tests/test_mock_provider.py" "$PKG/tests/test_mock_provider.py"
cp "$ROOT/tests/acceptance/test_provider_e2e.py" "$PKG/tests/acceptance/test_provider_e2e.py"
cp "$ROOT/docs/LINUX-TESTING.md" "$PKG/README-LINUX.md"
cp "$ROOT/docs/AUTH-AND-ENDPOINTS.md" "$PKG/AUTH-AND-ENDPOINTS.md"
cp "$ROOT/docs/STEALTH-OVERLAY.md" "$PKG/STEALTH-OVERLAY.md"
cp "$ROOT/docs/SHORTCUTS.md" "$PKG/SHORTCUTS.md"
cp "$ROOT/docs/A11Y-TREE-SHORTCUTS.md" "$PKG/A11Y-TREE-SHORTCUTS.md"
test ! -f "$ROOT/models/ggml-tiny.en.bin" || cp "$ROOT/models/ggml-tiny.en.bin" "$PKG/models/ggml-tiny.en.bin"
for f in "$ROOT/LICENSE" "$ROOT/COPYING"; do if [ -f "$f" ]; then cp "$f" "$PKG/"; break; fi; done
git -C "$ROOT" rev-parse HEAD > "$PKG/SOURCE-COMMIT.txt"
chmod 755 "$PKG/contextlens" "$PKG/bin/context-cli" "$PKG/check-dependencies.sh" "$PKG/install-dependencies-debian.sh"
OUT="$ROOT/dist/$NAME.tar.gz"
tar -C "$STAGING" -czf "$OUT" "$NAME"
(cd "$ROOT/dist" && shasum -a 256 "$NAME.tar.gz") > "$OUT.sha256"
echo "Linux archive: $OUT"
echo "Size: $(du -h "$OUT" | cut -f1)"
cat "$OUT.sha256"
