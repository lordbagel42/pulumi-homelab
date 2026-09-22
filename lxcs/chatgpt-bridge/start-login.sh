#!/bin/sh
# Run as root inside LXC 213 after forwarding localhost:1455 over SSH.
# Uses the normal browser sign-in; the process survives an SSH disconnect.
set -eu

if runuser -u chatgpt-bridge -- /usr/local/bin/codex login status; then
    exit 0
fi

unit=chatgpt-bridge-browser-login
log=/var/lib/chatgpt-bridge/browser-login.log
if ! systemctl is-active --quiet "$unit"; then
    install -o chatgpt-bridge -g chatgpt-bridge -m 0600 /dev/null "$log"
    systemd-run --quiet --collect --unit="$unit" --service-type=exec \
        --uid=chatgpt-bridge \
        --property=WorkingDirectory=/var/lib/chatgpt-bridge \
        --property=UMask=0077 \
        --property=RuntimeMaxSec=20min \
        --property="StandardOutput=append:$log" \
        --property="StandardError=append:$log" \
        /usr/local/bin/codex login
fi

attempt=0
until test -s "$log"; do
    attempt=$((attempt + 1))
    if test "$attempt" -ge 20; then
        echo "Login is starting; read $log in a moment."
        exit 0
    fi
    sleep 1
done
cat "$log"
