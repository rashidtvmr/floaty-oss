#!/usr/bin/env bash
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
systemctl --user start socket_stream_screen-test.service
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 2 http://127.0.0.1:18765/security-lab/screen-share-test.html >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.3
done
if [ "$ready" -ne 1 ]; then
  echo "SocketStream local test server did not start" >&2
  exit 1
fi
systemctl --user start socket_stream_real-app.service
xdg-open http://127.0.0.1:18765/security-lab/screen-share-test.html || true
