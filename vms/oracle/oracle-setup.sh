#!/bin/bash
# Sets up Oracle VM: Docker, Consul client, Traefik, DiscoPanel, cloudflared.
# Required env vars: CONSUL_VERSION, CONSUL_IP, TRAEFIK_VERSION, ORACLE_NB_IP, ORACLE_CF_TUNNEL_TOKEN
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl ca-certificates wget unzip

# --- Docker ---
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# --- Consul client ---
# Use host architecture (Oracle Free Tier may be ARM64)
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m | sed 's/aarch64/arm64/;s/x86_64/amd64/')
wget -q -O /tmp/consul.zip "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_${ARCH}.zip"
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
bind_addr  = "$ORACLE_NB_IP"
CONFEOF

# Register DiscoPanel — tagged "oracle" so only the Oracle Traefik picks it up
cat > /etc/consul.d/discopanel-service.json << SVCEOF
{
  "service": {
    "name": "discopanel",
    "address": "$ORACLE_NB_IP",
    "port": 8080,
    "tags": [
      "oracle",
      "traefik.enable=true",
      "traefik.http.routers.discopanel.rule=Host(\`panel.bagelindustries.com\`)",
      "traefik.http.routers.discopanel.entrypoints=web",
      "traefik.http.services.discopanel.loadbalancer.server.port=8080"
    ],
    "check": {
      "http": "http://127.0.0.1:8080",
      "interval": "15s",
      "timeout": "5s",
      "deregister_critical_service_after": "1m"
    }
  }
}
SVCEOF

chown -R consul:consul /etc/consul.d /var/lib/consul

cat > /etc/systemd/system/consul.service << 'CONSULEOF'
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
CONSULEOF

systemctl daemon-reload
systemctl enable consul
systemctl start consul

# --- Traefik config ---
# Constrained to "oracle"-tagged services only; local (optiplex) services are invisible to this instance
mkdir -p /etc/traefik

cat > /etc/traefik/traefik.yml << 'TRAEFIKEOF'
api:
  dashboard: true
  insecure: true

entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"
  traefik:
    address: ":8082"

providers:
  consulCatalog:
    prefix: traefik
    exposedByDefault: false
    constraints: "Tag(`oracle`)"
    endpoint:
      address: "127.0.0.1:8500"

log:
  level: INFO
TRAEFIKEOF

# --- Docker compose: Traefik + DiscoPanel + cloudflared ---
mkdir -p /app/data /app/backups /app/tmp /opt/oracle

cat > /opt/oracle/compose.yaml << COMPEOF
services:
  traefik:
    image: traefik:v${TRAEFIK_VERSION}
    container_name: traefik
    restart: unless-stopped
    network_mode: host
    volumes:
      - /etc/traefik:/etc/traefik:ro

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

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    network_mode: host
    command: tunnel --no-autoupdate run --token ${ORACLE_CF_TUNNEL_TOKEN}
COMPEOF

docker compose -f /opt/oracle/compose.yaml pull
docker compose -f /opt/oracle/compose.yaml up -d

cat > /etc/systemd/system/oracle-services.service << 'SVCEOF'
[Unit]
Description=Oracle Services (Traefik + DiscoPanel + cloudflared)
After=docker.service consul.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/oracle
ExecStart=/usr/bin/docker compose -f /opt/oracle/compose.yaml up -d
ExecStop=/usr/bin/docker compose -f /opt/oracle/compose.yaml down

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable oracle-services
