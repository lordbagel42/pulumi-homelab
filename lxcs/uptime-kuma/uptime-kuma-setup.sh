#!/bin/bash
# Sets up Uptime Kuma (Docker) + Consul client.
# Required env vars: CONSUL_VERSION, CONSUL_IP, UPTIME_KUMA_IP
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl wget unzip ca-certificates

# --- Docker ---
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable docker
systemctl start docker

# --- Consul client ---
if [ ! -f /usr/local/bin/consul ]; then
  wget -q -O /tmp/consul.zip \
    "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_amd64.zip"
  unzip -q -o /tmp/consul.zip -d /tmp
  mv /tmp/consul /usr/local/bin/consul && chmod +x /usr/local/bin/consul
  rm /tmp/consul.zip
fi

useradd -r -d /etc/consul.d -s /sbin/nologin consul 2>/dev/null || true
mkdir -p /etc/consul.d /var/lib/consul

cat > /etc/consul.d/consul.hcl << CONFEOF
datacenter = "homelab"
data_dir   = "/var/lib/consul"
log_level  = "INFO"
retry_join = ["$CONSUL_IP"]
bind_addr  = "$UPTIME_KUMA_IP"
CONFEOF

chown -R consul:consul /etc/consul.d /var/lib/consul

cat > /etc/systemd/system/consul.service << 'SVCEOF'
[Unit]
Description=Consul Agent
Documentation=https://www.consul.io/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=consul
Group=consul
ExecStart=/usr/local/bin/consul agent -config-dir=/etc/consul.d/
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable consul
systemctl restart consul

# --- Uptime Kuma ---
mkdir -p /opt/uptime-kuma/data

if ! docker ps --filter "name=uptime-kuma" --format "{{.Names}}" 2>/dev/null | grep -q uptime-kuma; then
  docker run -d \
    --name uptime-kuma \
    --restart unless-stopped \
    -p 3001:3001 \
    -v /opt/uptime-kuma/data:/app/data \
    louislam/uptime-kuma:latest
fi

echo "Uptime Kuma setup complete"
