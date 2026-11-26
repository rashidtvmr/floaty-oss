#!/bin/sh
# Reassemble the selected installer from the numbered parts in this checkout.
set -eu
case ${1:-} in
    macos) prefix=socket_stream_macos-arm64- ;;
    linux) prefix=socket_stream_linux-x86_64- ;;
    *) printf 'Usage: sh assemble.sh macos|linux\n' >&2; exit 2 ;;
esac
cd "$(dirname "$0")"
set -- "$prefix"*.sha256
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf 'Expected one checksum file for this platform.\n' >&2
    exit 1
fi
checksum=$1
archive=${checksum%.sha256}
set -- "$archive".part-??
if [ ! -f "$1" ]; then
    printf 'Installer parts not found.\n' >&2
    exit 1
fi
if [ -e "$archive" ] || [ -L "$archive" ]; then
    printf 'Refusing to overwrite %s.\n' "$archive" >&2
    exit 1
fi
tmp=$(mktemp "$archive.tmp.XXXXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
cat "$@" > "$tmp"
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$tmp")
else
    actual=$(shasum -a 256 "$tmp")
fi
expected=$(awk 'NR == 1 {print $1}' "$checksum")
if [ "${actual%% *}" != "$expected" ]; then
    printf 'SHA-256 mismatch; installer was not created.\n' >&2
    exit 1
fi
ln "$tmp" "$archive"
printf 'Verified installer: %s\n' "$archive"
