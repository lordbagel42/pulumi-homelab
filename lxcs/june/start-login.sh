#!/bin/sh
# Run as root in LXC 215 while localhost:1455 is forwarded from the browser host.
# This uses Codex's normal browser callback flow; device-code login is forbidden.
set -eu

codex=/opt/june/current/node_modules/.bin/codex
home=/var/lib/june
unit=june-codex-browser-login
log=/run/june-browser-login/output.log
runtime_path=/opt/node-v24.21.0/bin:/usr/local/bin:/usr/bin:/bin

test -x "$codex"
if runuser -u june -- env HOME="$home" CODEX_HOME="$home/.codex" PATH="$runtime_path" "$codex" login status; then
    exit 0
fi

if ! systemctl is-active --quiet "$unit"; then
    # The service account must not be able to replace a root-opened log with a
    # symlink. systemd opens this root-only file before dropping privileges.
    install -d -o root -g root -m 0700 /run/june-browser-login
    install -o root -g root -m 0600 /dev/null "$log"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemd-run --quiet --collect --unit="$unit" --service-type=exec \
        --uid=june --gid=june \
        --working-directory="$home" \
        --setenv="HOME=$home" \
        --setenv="CODEX_HOME=$home/.codex" \
        --setenv="PATH=$runtime_path" \
        --property=UMask=0077 \
        --property=RuntimeMaxSec=20min \
        --property=KillMode=control-group \
        --property="StandardOutput=append:$log" \
        --property="StandardError=append:$log" \
        "$codex" login
fi

attempt=0
until test -s "$log"; do
    attempt=$((attempt + 1))
    if test "$attempt" -ge 20; then
        echo "Browser login is starting; read $log in a moment."
        exit 0
    fi
    sleep 1
done
cat "$log"
