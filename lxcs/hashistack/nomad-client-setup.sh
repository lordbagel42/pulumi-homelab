#!/bin/bash
# Installs Docker, CNI plugins, Consul client, and Nomad client on a VM.
# Required env vars: CONSUL_VERSION, CONSUL_IP, NOMAD_VERSION, NOMAD_CLIENT_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq wget curl unzip ca-certificates gnupg lsb-release

# --- Docker ---
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

DISTRO_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${DISTRO_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io

systemctl enable docker
systemctl start docker

# --- CNI plugins ---
CNI_VERSION="1.5.1"
mkdir -p /opt/cni/bin
wget -q -O /tmp/cni-plugins.tgz "https://github.com/containernetworking/plugins/releases/download/v${CNI_VERSION}/cni-plugins-linux-amd64-v${CNI_VERSION}.tgz"
tar -xzf /tmp/cni-plugins.tgz -C /opt/cni/bin
rm /tmp/cni-plugins.tgz

# br_netfilter must be loaded before the sysctl key exists
modprobe br_netfilter 2>/dev/null || true
echo 'br_netfilter' > /etc/modules-load.d/br_netfilter.conf
echo 'net.bridge.bridge-nf-call-iptables = 1' > /etc/sysctl.d/99-nomad-cni.conf
sysctl -p /etc/sysctl.d/99-nomad-cni.conf

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
bind_addr  = "$NOMAD_CLIENT_IP"
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

# --- Nomad client ---
wget -q -O /tmp/nomad.zip "https://releases.hashicorp.com/nomad/${NOMAD_VERSION}/nomad_${NOMAD_VERSION}_linux_amd64.zip"
unzip -q -o /tmp/nomad.zip -d /tmp
mv /tmp/nomad /usr/local/bin/nomad && chmod +x /usr/local/bin/nomad
rm /tmp/nomad.zip

mkdir -p /etc/nomad.d /var/lib/nomad

cat > /etc/nomad.d/nomad.hcl << 'NOMADEOF'
datacenter = "dc1"
data_dir   = "/var/lib/nomad"
log_level  = "INFO"

client {
  enabled  = true
  cni_path = "/opt/cni/bin"
}

consul {
  address = "127.0.0.1:8500"
}

plugin "docker" {
  config {
    allow_privileged = false
  }
}
NOMADEOF

cat > /etc/systemd/system/nomad.service << 'SVCEOF'
[Unit]
Description=Nomad Client
Documentation=https://www.nomadproject.io/
After=network-online.target consul.service docker.service
Wants=network-online.target

[Service]
Type=exec
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
