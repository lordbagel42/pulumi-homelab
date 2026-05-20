#!/bin/bash
# Installs Docker, runs DiscoPanel, and registers it with Consul for Traefik routing.
# Required env vars: DISCOPANEL_IP, CONSUL_IP, CONSUL_VERSION
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl ca-certificates wget unzip

# --- Docker ---
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable docker
systemctl start docker

# --- DiscoPanel ---
mkdir -p /app/data /app/backups /app/tmp /opt/discopanel

cat > /opt/discopanel/compose.yaml << 'COMPEOF'
services:
  discopanel:
    image: nickheyer/discopanel:latest
    container_name: discopanel
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /app/data:/app/data
      - /app/backups:/app/backups
      - /app/tmp:/app/tmp
    environment:
      - DISCOPANEL_DATA_DIR=/app/data
      - DISCOPANEL_HOST_DATA_PATH=/app/data
      - TZ=UTC
    extra_hosts:
      - "host.docker.internal:host-gateway"
COMPEOF

docker compose -f /opt/discopanel/compose.yaml pull
docker compose -f /opt/discopanel/compose.yaml up -d

cat > /etc/systemd/system/discopanel.service << 'SVCEOF'
[Unit]
Description=DiscoPanel Minecraft Manager
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/discopanel
ExecStart=/usr/bin/docker compose -f /opt/discopanel/compose.yaml up -d
ExecStop=/usr/bin/docker compose -f /opt/discopanel/compose.yaml down

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable discopanel

# --- Consul client ---
wget -q -O /tmp/consul.zip "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_amd64.zip"
unzip -q -o /tmp/consul.zip -d /tmp
mv /tmp/consul /usr/local/bin/consul && chmod +x /usr/local/bin/consul
rm /tmp/consul.zip

useradd -r -d /etc/consul.d -s /sbin/nologin consul 2>/dev/null || true
mkdir -p /etc/consul.d /var/lib/consul

cat > /etc/consul.d/consul.hcl << CONFEOF
datacenter = "dc1"
data_dir   = "/var/lib/consul"
log_level  = "INFO"
retry_join = ["$CONSUL_IP"]
bind_addr  = "$DISCOPANEL_IP"
CONFEOF

cat > /etc/consul.d/discopanel-service.json << SVCEOF
{
  "service": {
    "name": "discopanel",
    "address": "$DISCOPANEL_IP",
    "port": 8080,
    "tags": [
      "traefik.enable=true",
      "traefik.http.routers.discopanel.rule=Host(\`panel.bagelindustries.com\`)",
      "traefik.http.routers.discopanel.entrypoints=web",
      "traefik.http.services.discopanel.loadbalancer.server.port=8080"
    ],
    "check": {
      "http": "http://$DISCOPANEL_IP:8080",
      "interval": "15s",
      "timeout": "5s",
      "deregister_critical_service_after": "1m"
    }
  }
}
SVCEOF

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
