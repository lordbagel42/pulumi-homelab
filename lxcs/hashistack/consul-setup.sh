#!/bin/bash
# Installs and starts a single-node Consul server.
# Required env vars: CONSUL_VERSION, CONSUL_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget unzip

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
server     = true
bootstrap_expect = 1
ui_config { enabled = true }
client_addr = "0.0.0.0"
bind_addr   = "$CONSUL_IP"

connect {
  enabled = true
}
CONFEOF

chown -R consul:consul /etc/consul.d /var/lib/consul

cat > /etc/systemd/system/consul.service << 'SVCEOF'
[Unit]
Description=Consul
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
sleep 3
chown -R consul:consul /var/lib/consul

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

