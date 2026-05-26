#!/bin/bash
# Sets up Authentik (Docker Compose) + Consul client + service registration.
# Required env vars: CONSUL_VERSION, CONSUL_IP, AUTHENTIK_IP,
#                    PG_PASS, SECRET_KEY, BOOTSTRAP_PASSWORD, BOOTSTRAP_TOKEN
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl wget unzip jq ca-certificates gnupg

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
bind_addr  = "$AUTHENTIK_IP"
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

# --- Authentik Docker Compose ---
mkdir -p /opt/authentik

cat > /opt/authentik/.env << ENVEOF
PG_PASS=$PG_PASS
SECRET_KEY=$SECRET_KEY
AUTHENTIK_BOOTSTRAP_PASSWORD=$BOOTSTRAP_PASSWORD
AUTHENTIK_BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
AUTHENTIK_BOOTSTRAP_EMAIL=raygenrrupe@gmail.com
ENVEOF
chmod 600 /opt/authentik/.env

cat > /opt/authentik/docker-compose.yml << 'COMPOSEEOF'
version: "3.4"

services:
  postgresql:
    image: docker.io/library/postgres:16-alpine
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -d $${POSTGRES_DB} -U $${POSTGRES_USER}"]
      start_period: 20s
      interval: 30s
      retries: 5
      timeout: 5s
    volumes:
      - database:/var/lib/postgresql/data
    environment:
      POSTGRES_PASSWORD: ${PG_PASS}
      POSTGRES_USER: authentik
      POSTGRES_DB: authentik

  redis:
    image: docker.io/library/redis:alpine
    command: --save 60 1 --loglevel warning
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "redis-cli ping | grep PONG"]
      start_period: 20s
      interval: 30s
      retries: 5
      timeout: 3s
    volumes:
      - redis:/data

  server:
    image: ghcr.io/goauthentik/server:latest
    restart: unless-stopped
    command: server
    environment:
      AUTHENTIK_REDIS__HOST: redis
      AUTHENTIK_POSTGRESQL__HOST: postgresql
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: ${PG_PASS}
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_SECRET_KEY: ${SECRET_KEY}
      AUTHENTIK_BOOTSTRAP_PASSWORD: ${AUTHENTIK_BOOTSTRAP_PASSWORD}
      AUTHENTIK_BOOTSTRAP_TOKEN: ${AUTHENTIK_BOOTSTRAP_TOKEN}
      AUTHENTIK_BOOTSTRAP_EMAIL: ${AUTHENTIK_BOOTSTRAP_EMAIL}
      AUTHENTIK_ERROR_REPORTING__ENABLED: "false"
      AUTHENTIK_HOST: "http://auth.bagelindustries.com"
      AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS: "192.168.0.0/16"
    volumes:
      - media:/media
      - custom-templates:/templates
    ports:
      - "9000:9000"
      - "9443:9443"
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    image: ghcr.io/goauthentik/server:latest
    restart: unless-stopped
    command: worker
    environment:
      AUTHENTIK_REDIS__HOST: redis
      AUTHENTIK_POSTGRESQL__HOST: postgresql
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: ${PG_PASS}
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_SECRET_KEY: ${SECRET_KEY}
      AUTHENTIK_ERROR_REPORTING__ENABLED: "false"
    user: root
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - media:/media
      - certs:/certs
      - custom-templates:/templates
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_healthy

volumes:
  database:
  redis:
  media:
  certs:
  custom-templates:
COMPOSEEOF

cd /opt/authentik
docker compose up -d

# Wait for Authentik to be ready (up to 10 minutes)
echo "Waiting for Authentik to start..."
for i in $(seq 1 60); do
  if curl -sf "http://localhost:9000/-/health/ready/" > /dev/null 2>&1; then
    echo "Authentik ready after ${i} attempts"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "Authentik did not become ready in time" >&2
    docker compose logs --tail=50 >&2
    exit 1
  fi
  echo "  attempt $i/60 (waiting 10s)..."
  sleep 10
done

