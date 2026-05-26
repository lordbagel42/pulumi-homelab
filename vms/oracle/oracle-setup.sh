#!/bin/bash
# Sets up Oracle VM: Docker, Consul client, Traefik config, Nomad client.
# Required env vars: CONSUL_VERSION, CONSUL_IP, TRAEFIK_VERSION, NOMAD_VERSION,
#                    NOMAD_SERVER_IP, ORACLE_NB_IP
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl ca-certificates wget unzip

# --- Docker ---
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable docker
systemctl start docker

# --- ARCH detection (Oracle Free Tier may be ARM64) ---
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m | sed 's/aarch64/arm64/;s/x86_64/amd64/')

# --- Consul client ---
if [ ! -f /usr/local/bin/consul ]; then
  wget -q -O /tmp/consul.zip \
    "https://releases.hashicorp.com/consul/${CONSUL_VERSION}/consul_${CONSUL_VERSION}_linux_${ARCH}.zip"
  unzip -q -o /tmp/consul.zip -d /tmp
  mv /tmp/consul /usr/local/bin/consul && chmod +x /usr/local/bin/consul
  rm /tmp/consul.zip
fi

useradd -r -d /etc/consul.d -s /sbin/nologin consul 2>/dev/null || true
mkdir -p /etc/consul.d /var/lib/consul

cat > /etc/consul.d/consul.hcl << CONFEOF
datacenter = "dc1"
data_dir   = "/var/lib/consul"
log_level  = "INFO"
retry_join = ["$CONSUL_IP"]
bind_addr  = "$ORACLE_NB_IP"
CONFEOF

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

# --- CNI plugins (required for Nomad bridge networking) ---
CNI_VERSION="1.5.1"
if [ ! -f /opt/cni/bin/bridge ]; then
  mkdir -p /opt/cni/bin
  wget -q -O /tmp/cni-plugins.tgz \
    "https://github.com/containernetworking/plugins/releases/download/v${CNI_VERSION}/cni-plugins-linux-${ARCH}-v${CNI_VERSION}.tgz"
  tar -xzf /tmp/cni-plugins.tgz -C /opt/cni/bin
  rm /tmp/cni-plugins.tgz
fi

modprobe br_netfilter 2>/dev/null || true
echo 'br_netfilter' > /etc/modules-load.d/br_netfilter.conf
echo 'net.bridge.bridge-nf-call-iptables = 1' > /etc/sysctl.d/99-nomad-cni.conf
sysctl -p /etc/sysctl.d/99-nomad-cni.conf 2>/dev/null || true

# --- Data directories for Pelican Panel ---
mkdir -p /app/pelican/mysql /app/pelican/data /app/pelican/logs

# --- Nomad client ---
if [ ! -f /usr/local/bin/nomad ]; then
  wget -q -O /tmp/nomad.zip \
    "https://releases.hashicorp.com/nomad/${NOMAD_VERSION}/nomad_${NOMAD_VERSION}_linux_${ARCH}.zip"
  unzip -q -o /tmp/nomad.zip -d /tmp
  mv /tmp/nomad /usr/local/bin/nomad && chmod +x /usr/local/bin/nomad
  rm /tmp/nomad.zip
fi

mkdir -p /etc/nomad.d /var/lib/nomad

# Oracle Cloud VMs (especially ARM Ampere) report 0 MHz to Nomad; compute manually.
CPU_MHZ=$(lscpu | grep -E '^CPU max MHz:|^CPU MHz:' | head -1 | awk '{print $NF}' | cut -d. -f1)
[ -z "$CPU_MHZ" ] && CPU_MHZ=3000
CPU_CORES=$(nproc)
CPU_TOTAL=$((CPU_MHZ * CPU_CORES))

cat > /etc/nomad.d/nomad.hcl << NOMADEOF
datacenter = "dc1"
data_dir   = "/var/lib/nomad"
log_level  = "INFO"
name       = "oracle"

# Bind to all interfaces; advertise the NetBird IP so the homelab server
# can reach this client over the VPN tunnel.
bind_addr = "0.0.0.0"

advertise {
  http = "$ORACLE_NB_IP"
  rpc  = "$ORACLE_NB_IP"
  serf = "$ORACLE_NB_IP"
}

client {
  enabled           = true
  node_class        = "oracle"
  cpu_total_compute = $CPU_TOTAL
  cni_path          = "/opt/cni/bin"

  server_join {
    retry_join = ["$NOMAD_SERVER_IP:4647"]
  }
}

consul {
  address = "127.0.0.1:8500"
}

plugin "docker" {
  config {
    volumes {
      enabled = true
    }
  }
}
NOMADEOF

cat > /etc/systemd/system/nomad.service << 'NOMADSVCEOF'
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
NOMADSVCEOF

systemctl daemon-reload
systemctl enable nomad
systemctl restart nomad
echo "Oracle node setup complete"
