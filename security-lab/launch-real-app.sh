#!/usr/bin/env bash
set -eu
SERVICE=socket_stream_real-app.service
if systemctl --user is-active --quiet "$SERVICE"; then
 systemctl --user kill --kill-whom=main --signal=USR1 "$SERVICE"
else
 systemctl --user start "$SERVICE"
fi
