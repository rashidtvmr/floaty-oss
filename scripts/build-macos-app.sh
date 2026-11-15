#!/bin/sh
# Build a self-contained SocketStream.app for the current Mac.
#
# Layout:
#   Contents/MacOS/SocketStream              AppKit shell
#   Contents/MacOS/socket_stream              Rust core
#   Contents/Resources/target/native-helpers native AX/OCR/audio helpers
#   Contents/Resources/scripts              Swift helper sources (fallback)
#
# By default the bundle is installed to ~/Applications/SocketStream.app. Pass
# --no-install to build dist/SocketStream.app only.
#
# Signing: the bundle is signed with a stable local identity when one exists so
# that macOS TCC grants (Accessibility, Screen Recording) survive rebuilds. TCC
# stores a code-signing requirement per grant; ad-hoc signing pins it to the
# binary's cdhash, which changes on every rebuild, so the grant silently stops
# matching and the app reports "not granted" while System Settings still shows
# the toggle on. Run scripts/create-macos-signing-cert.sh once to create the
# identity, or pass --sign-identity / set SOCKET_STREAM_SIGNING_IDENTITY. When no
# identity is available the script falls back to ad-hoc and warns.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROFILE="${SOCKET_STREAM_PROFILE:-release}"
APP_NAME="SocketStream"
DIST_DIR=${SOCKET_STREAM_DIST_DIR:-"$ROOT/dist"}
BUNDLE="$DIST_DIR/$APP_NAME.app"
INSTALL=1
SIGN_IDENTITY="${SOCKET_STREAM_SIGNING_IDENTITY:-SocketStream Local Signing}"

for argument in "$@"; do
  case "$argument" in
    --no-install) INSTALL=0 ;;
    --install) INSTALL=1 ;;
    --debug) PROFILE="debug" ;;
    --release) PROFILE="release" ;;
    --sign-identity=*) SIGN_IDENTITY="${argument#--sign-identity=}" ;;
    --sign-identity)
      echo "use --sign-identity=NAME" >&2
      exit 2
      ;;
    *)
      echo "Usage: $0 [--no-install] [--debug|--release] [--sign-identity=NAME]" >&2
      exit 2
      ;;
  esac
done

echo "Building socket_stream ($PROFILE)…"
cargo build --manifest-path "$ROOT/Cargo.toml" -p socket_stream --profile "$PROFILE"
CLI="$ROOT/target/$PROFILE/socket_stream"
[ -x "$CLI" ] || { echo "socket_stream was not built at $CLI" >&2; exit 1; }

echo "Building native helpers…"
"$ROOT/scripts/build-macos-helpers.sh"
MOONSHINE_PYTHON=${SOCKET_STREAM_BUILD_PYTHON:-python3.12}
"$MOONSHINE_PYTHON" "$ROOT/scripts/build-moonshine.py"
GUI="$ROOT/target/native-helpers/socket_stream_gui"
for helper in socket_stream_a11y socket_stream_ocr socket_stream_system_audio socket_stream_audio_stream; do
  [ -x "$ROOT/target/native-helpers/$helper" ] || {
    echo "missing native helper $helper" >&2
    exit 1
  }
done

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS"
mkdir -p "$BUNDLE/Contents/Resources/scripts"
mkdir -p "$BUNDLE/Contents/Resources/target/native-helpers"

cp "$GUI" "$BUNDLE/Contents/MacOS/$APP_NAME"
cp "$CLI" "$BUNDLE/Contents/MacOS/socket_stream"
cp "$ROOT/target/native-helpers/socket_stream_a11y" "$BUNDLE/Contents/Resources/target/native-helpers/"
cp "$ROOT/target/native-helpers/socket_stream_ocr" "$BUNDLE/Contents/Resources/target/native-helpers/"
cp "$ROOT/target/native-helpers/socket_stream_system_audio" "$BUNDLE/Contents/Resources/target/native-helpers/"
cp "$ROOT/target/native-helpers/socket_stream_audio_stream" "$BUNDLE/Contents/Resources/target/native-helpers/"
cp -R "$ROOT/target/native-helpers/moonshine" "$BUNDLE/Contents/Resources/target/native-helpers/"
cp "$ROOT"/scripts/macos-*.swift "$BUNDLE/Contents/Resources/scripts/"
if [ -d "$ROOT/models" ]; then
  cp -R "$ROOT/models" "$BUNDLE/Contents/Resources/models"
fi

cat >"$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>SocketStream</string>
    <key>CFBundleExecutable</key>
    <string>SocketStream</string>
    <key>CFBundleIdentifier</key>
    <string>dev.socket_stream.app</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>SocketStream</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>0.1.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.developer-tools</string>
    <key>LSMinimumSystemVersion</key>
    <string>15.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>SocketStream records microphone audio only when you explicitly start an authorized audio practice session.</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>SocketStream records system audio only when you explicitly start an authorized audio practice session.</string>
    <key>NSHumanReadableCopyright</key>
    <string>SocketStream — authorized practice tooling.</string>
</dict>
</plist>
PLIST

/usr/bin/plutil -lint "$BUNDLE/Contents/Info.plist" >/dev/null

echo "Signing…"
# Prefer a stable identity so TCC grants survive rebuilds. Ad-hoc signing pins
# the grant to a cdhash that changes on every build.
if [ -z "$SIGN_IDENTITY" ] || [ "$SIGN_IDENTITY" = "-" ]; then
  SIGN_IDENTITY="-"
elif ! security find-identity -p codesigning 2>/dev/null | grep -qF "\"$SIGN_IDENTITY\""; then
  echo "  warning: signing identity '$SIGN_IDENTITY' not found; using ad-hoc." >&2
  echo "           Accessibility/Screen Recording will reset on every rebuild." >&2
  echo "           Run scripts/create-macos-signing-cert.sh once to fix this." >&2
  SIGN_IDENTITY="-"
fi

if [ "$SIGN_IDENTITY" = "-" ]; then
  echo "  identity: ad-hoc (TCC grants reset on rebuild)"
else
  echo "  identity: $SIGN_IDENTITY (TCC grants survive rebuilds)"
fi

# `codesign --deep` only signs code in the standard nested-code locations
# (MacOS/, Frameworks/, PlugIns/, ...). The helpers live under Contents/Resources,
# so --deep leaves their build-time ad-hoc signatures in place and their TCC
# requirement stays pinned to a cdhash. Sign them explicitly first.
for helper in "$BUNDLE/Contents/Resources/target/native-helpers/"*; do
  [ -f "$helper" ] || continue
  codesign --force --sign "$SIGN_IDENTITY" "$helper" 2>&1 | sed 's/^/  /' || true
done

# The embedded Python/Moonshine code lives in Resources. Sign each Mach-O
# library as well as the launcher; --deep alone does not traverse this layout.
find "$BUNDLE/Contents/Resources/target/native-helpers/moonshine" -type f -print | while IFS= read -r binary; do
  if file "$binary" | grep -q 'Mach-O'; then
    codesign --force --sign "$SIGN_IDENTITY" "$binary"
  fi
done

codesign --force --deep --sign "$SIGN_IDENTITY" "$BUNDLE" 2>&1 | sed 's/^/  /' || {
  echo "warning: signing failed; macOS may re-prompt for permissions" >&2
}
codesign --verify --deep --strict "$BUNDLE" 2>&1 | sed 's/^/  /' || true

echo "Built $BUNDLE"

if [ "$INSTALL" -eq 1 ]; then
  INSTALL_DIR="${SOCKET_STREAM_INSTALL_DIR:-$HOME/Applications}"
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/$APP_NAME.app"
  ditto "$BUNDLE" "$INSTALL_DIR/$APP_NAME.app"
  echo "Installed $INSTALL_DIR/$APP_NAME.app"
fi
