#!/bin/bash
# Installs and starts Traefik with Consul catalog provider.
# Required env vars: CONSUL_IP, TRAEFIK_VERSION
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget libcap2-bin

wget -q -O /tmp/traefik.tar.gz "https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}/traefik_v${TRAEFIK_VERSION}_linux_amd64.tar.gz"
tar -xzf /tmp/traefik.tar.gz -C /tmp traefik
mv /tmp/traefik /usr/local/bin/traefik && chmod +x /usr/local/bin/traefik
setcap 'cap_net_bind_service=+ep' /usr/local/bin/traefik
rm /tmp/traefik.tar.gz

useradd -r -d /etc/traefik -s /sbin/nologin traefik 2>/dev/null || true
mkdir -p /etc/traefik

cat > /etc/traefik/traefik.yml << CONFEOF
api:
  dashboard: true
  insecure: true

entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"
  traefik:
    address: ":8080"

providers:
  consulCatalog:
    prefix: traefik
    exposedByDefault: false
    constraints: "!Tag(\`oracle\`)"
    endpoint:
      address: "${CONSUL_IP}:8500"

log:
  level: INFO
CONFEOF

chown -R traefik:traefik /etc/traefik

cat > /etc/systemd/system/traefik.service << 'SVCEOF'
[Unit]
Description=Traefik
Documentation=https://doc.traefik.io/traefik/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=traefik
Group=traefik
ExecStart=/usr/local/bin/traefik --configFile=/etc/traefik/traefik.yml
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable traefik
systemctl restart traefik
