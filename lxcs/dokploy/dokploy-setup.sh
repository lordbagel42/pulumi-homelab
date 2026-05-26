#!/bin/bash
# Installs Consul client (for Traefik service discovery) and Dokploy.
# Required env vars: CONSUL_VERSION, CONSUL_IP, DOKPLOY_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl wget unzip

# --- Consul client ---
wget -q -O /tmp/consul.zip "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_amd64.zip"
unzip -q -o /tmp/consul.zip -d /tmp
mv /tmp/consul /usr/local/bin/consul && chmod +x /usr/local/bin/consul
rm /tmp/consul.zip

useradd -r -d /etc/consul.d -s /sbin/nologin consul 2>/dev/null || true
mkdir -p /etc/consul.d /var/lib/consul

cat > /etc/consul.d/consul.hcl << CONFEOF
datacenter = "homelab"
data_dir   = "/var/lib/consul"
log_level  = "INFO"
retry_join = ["$CONSUL_IP"]
bind_addr  = "$DOKPLOY_IP"
CONFEOF

# Register Dokploy as a service with Traefik tags
cat > /etc/consul.d/dokploy-service.json << SVCEOF
{
  "service": {
    "name": "dokploy",
    "address": "$DOKPLOY_IP",
    "port": 3000,
    "tags": [
      "traefik.enable=true",
      "traefik.http.routers.dokploy.rule=Host(\`dokploy.raygen.dev\`)",
      "traefik.http.routers.dokploy.entrypoints=web"
    ],
    "check": {
      "http": "http://$DOKPLOY_IP:3000",
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

# --- Dokploy ---
if docker ps --filter "name=dokploy" --format "{{.Names}}" 2>/dev/null | grep -q dokploy; then
    echo "Dokploy already running, skipping install"
else
    wget -q -O /tmp/dokploy-install.sh https://dokploy.com/install.sh
    bash /tmp/dokploy-install.sh
    rm /tmp/dokploy-install.sh
fi
