#!/bin/bash
# Installs cloudflared and runs it as a systemd service using a tunnel token.
# Required env vars: CLOUDFLARE_TUNNEL_TOKEN
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget

wget -q -O /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
dpkg -i /tmp/cloudflared.deb
rm /tmp/cloudflared.deb

mkdir -p /etc/cloudflared
printf 'TUNNEL_TOKEN=%s\n' "$CLOUDFLARE_TUNNEL_TOKEN" > /etc/cloudflared/env
chmod 600 /etc/cloudflared/env

cat > /etc/systemd/system/cloudflared.service << 'SVCEOF'
[Unit]
Description=Cloudflare Tunnel
Documentation=https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/cloudflared/env
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token ${TUNNEL_TOKEN}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable cloudflared
systemctl start cloudflared
