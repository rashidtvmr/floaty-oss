#!/bin/sh
# Clear stale TCC rows for SocketStream so the next grant is stored against the
# current signature.
#
# Use this when System Settings shows the Accessibility or Screen Recording
# toggle as enabled but the app still reports "not granted". That mismatch means
# the stored code-signing requirement (csreq) no longer matches the running
# binary, which happens when the app was rebuilt with a different signature
# (ad-hoc signing) or was signed with a different certificate.
#
# tccutil only removes the row; it never grants anything. The user still grants
# the permission in System Settings, and the app never edits the TCC database.
set -eu

BUNDLE_ID="${SOCKET_STREAM_BUNDLE_ID:-dev.socket_stream.app}"

for service in Accessibility ScreenCapture; do
  if tccutil reset "$service" "$BUNDLE_ID" >/dev/null 2>&1; then
    echo "reset $service for $BUNDLE_ID"
  else
    echo "could not reset $service for $BUNDLE_ID (already clear?)" >&2
  fi
done

echo
echo "Next: quit and reopen SocketStream, click Request permissions, and grant both"
echo "toggles in System Settings. With a stable signing identity the grant will"
echo "survive future rebuilds."
