#!/bin/bash
# Installs a Consul client and single-node Nomad server.
# Required env vars: CONSUL_VERSION, CONSUL_IP, NOMAD_VERSION, NOMAD_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget unzip

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
bind_addr  = "$NOMAD_IP"
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

echo "Waiting for Consul to be healthy..."
for i in $(seq 1 40); do
  if consul members 2>/dev/null | grep -q "alive"; then
    echo "Consul is ready"
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done

# --- Nomad server ---
wget -q -O /tmp/nomad.zip "https://releases.hashicorp.com/nomad/${NOMAD_VERSION}/nomad_${NOMAD_VERSION}_linux_amd64.zip"
unzip -q -o /tmp/nomad.zip -d /tmp
mv /tmp/nomad /usr/local/bin/nomad && chmod +x /usr/local/bin/nomad
rm /tmp/nomad.zip

useradd -r -d /etc/nomad.d -s /sbin/nologin nomad 2>/dev/null || true
mkdir -p /etc/nomad.d /var/lib/nomad

cat > /etc/nomad.d/nomad.hcl << 'NOMADEOF'
datacenter = "dc1"
data_dir   = "/var/lib/nomad"
log_level  = "INFO"

server {
  enabled          = true
  bootstrap_expect = 1
}

consul {
  address = "127.0.0.1:8500"
}
NOMADEOF

chown -R nomad:nomad /etc/nomad.d /var/lib/nomad

cat > /etc/systemd/system/nomad.service << 'SVCEOF'
[Unit]
Description=Nomad
Documentation=https://www.nomadproject.io/
After=network-online.target consul.service
Wants=network-online.target

[Service]
Type=exec
User=nomad
Group=nomad
ExecStart=/usr/local/bin/nomad agent -config=/etc/nomad.d/nomad.hcl
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
Restart=on-failure
LimitNOFILE=65536
LimitNPROC=infinity
TasksMax=infinity

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable nomad
systemctl restart nomad

# Persistent route so homelab can reach oracle's NetBird IP (100.64.0.0/10 via optiplex)
cat > /etc/systemd/system/netbird-route.service << 'ROUTEOF'
[Unit]
Description=Add NetBird route via optiplex
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/sbin/ip route replace 100.64.0.0/10 via 192.168.0.10
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
ROUTEOF

systemctl daemon-reload
systemctl enable netbird-route
systemctl start netbird-route

cat > /etc/consul.d/nomad-ui-service.json << 'SVCDEF'
{
  "service": {
    "name": "nomad-ui",
    "port": 4646,
    "tags": [
      "traefik.enable=true",
      "traefik.http.routers.nomad-ui.rule=Host(`nomad.bagelindustries.com`)",
      "traefik.http.routers.nomad-ui.entrypoints=web",
      "traefik.http.routers.nomad-ui.middlewares=authentik@consulcatalog",
      "traefik.http.services.nomad-ui.loadbalancer.server.port=4646"
    ]
  }
}
SVCDEF
chown consul:consul /etc/consul.d/nomad-ui-service.json
consul reload
