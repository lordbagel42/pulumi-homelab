#!/bin/bash
# Installs a Consul client and single-node Nomad server.
# Required env vars: CONSUL_VERSION, CONSUL_IP, NOMAD_VERSION, NOMAD_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget unzip

# --- Consul client ---
wget -q -O /tmp/consul.zip "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_amd64.zip"
unzip -q /tmp/consul.zip -d /tmp
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
Type=notify
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
systemctl start consul

sleep 5

# --- Nomad server ---
wget -q -O /tmp/nomad.zip "https://releases.hashicorp.com/nomad/${NOMAD_VERSION}/nomad_${NOMAD_VERSION}_linux_amd64.zip"
unzip -q /tmp/nomad.zip -d /tmp
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
systemctl start nomad
