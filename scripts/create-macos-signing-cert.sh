#!/bin/sh
# Create a stable, local, self-signed code-signing identity.
#
# Why this exists
# ---------------
# macOS TCC (the permission database behind Accessibility and Screen Recording)
# does not store "this app is allowed". It stores a *code signing requirement*
# (csreq) and re-evaluates it every time the app asks for a permission.
#
# With ad-hoc signing (`codesign --sign -`) the stored requirement is
# `cdhash H"..."`. The cdhash is a hash of the built binary, so it changes on
# every rebuild. After a rebuild the stored requirement no longer matches the
# running app, TCC silently denies, and the app reports "not granted" even
# though the System Settings toggle is still on.
#
# Signing with a certificate moves the requirement to
# `identifier "..." and certificate root = H"..."`, which is stable across
# rebuilds. The certificate does not need to be trusted by Apple or by the
# system trust store; TCC only compares the certificate hash embedded in the
# signature. A local self-signed certificate is enough.
#
# This script is idempotent: it does nothing if the identity already exists.
# Remove it later with:
#   security delete-identity -c "SocketStream Local Signing"
set -eu

CN="${SOCKET_STREAM_SIGNING_IDENTITY:-SocketStream Local Signing}"
KEYCHAIN="${SOCKET_STREAM_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"

if security find-identity -p codesigning 2>/dev/null | grep -qF "\"$CN\""; then
  echo "signing identity already present: $CN"
  exit 0
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl not found; cannot create a signing identity" >&2
  exit 1
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/socket_stream_cert.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
P12PASS=$(openssl rand -hex 16)

# A leaf code-signing certificate. LibreSSL (the /usr/bin/openssl on macOS)
# supports -addext.
openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
  -subj "/CN=$CN/O=SocketStream/C=US" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" \
  -addext "basicConstraints=critical,CA:false" >/dev/null 2>&1

# LibreSSL's pkcs12 already emits the legacy algorithms macOS Keychain needs,
# so the OpenSSL -legacy flag is neither present nor required here.
openssl pkcs12 -export -in "$WORK/cert.pem" -inkey "$WORK/key.pem" \
  -out "$WORK/identity.p12" -password "pass:$P12PASS" >/dev/null 2>&1

# -T authorizes codesign to use the key without an interactive prompt.
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$P12PASS" \
  -T /usr/bin/codesign -T /usr/bin/security >/dev/null

if ! security find-identity -p codesigning 2>/dev/null | grep -qF "\"$CN\""; then
  echo "certificate imported but not usable for code signing" >&2
  exit 1
fi

echo "created signing identity: $CN"
