#!/bin/sh
# Create a ZIP that preserves the signed application bundle and its symlinks.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR=${SOCKET_STREAM_DIST_DIR:-"$ROOT/dist"}
mkdir -p "$DIST_DIR"
DIST_DIR=$(CDPATH= cd -- "$DIST_DIR" && pwd)
STAGING=$(mktemp -d "$DIST_DIR/.macos-package.XXXXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM

SOCKET_STREAM_DIST_DIR="$STAGING" "$ROOT/scripts/build-macos-app.sh" --no-install
BUNDLE="$STAGING/SocketStream.app"
codesign --verify --deep --strict "$BUNDLE"
"$BUNDLE/Contents/MacOS/SocketStream" --self-test
NAME="socket_stream_macos-$(uname -m)-$(date +%Y%m%d).zip"
ditto -c -k --keepParent "$BUNDLE" "$STAGING/$NAME"
(cd "$STAGING" && shasum -a 256 "$NAME") > "$STAGING/$NAME.sha256"
mv "$STAGING/$NAME" "$STAGING/$NAME.sha256" "$DIST_DIR/"
printf 'macOS archive: %s\n' "$DIST_DIR/$NAME"
