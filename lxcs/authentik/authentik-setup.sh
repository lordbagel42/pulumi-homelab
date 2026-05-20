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
datacenter = "dc1"
data_dir   = "/var/lib/consul"
log_level  = "INFO"
retry_join = ["$CONSUL_IP"]
bind_addr  = "$AUTHENTIK_IP"
CONFEOF

# Authentik service + forwardAuth middleware definition for Traefik
cat > /etc/consul.d/authentik-service.json << SVCDEF
{
  "service": {
    "name": "authentik",
    "port": 9000,
    "tags": [
      "traefik.enable=true",
      "traefik.http.routers.authentik.rule=Host(\`authentik.bagelindustries.com\`)",
      "traefik.http.routers.authentik.entrypoints=web",
      "traefik.http.services.authentik.loadbalancer.server.port=9000",
      "traefik.http.middlewares.authentik.forwardauth.address=http://$AUTHENTIK_IP:9000/outpost.goauthentik.io/auth/traefik",
      "traefik.http.middlewares.authentik.forwardauth.trustForwardHeader=true",
      "traefik.http.middlewares.authentik.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-uid,X-authentik-jwt,X-authentik-meta-jwks,X-authentik-meta-outpost,X-authentik-meta-provider,X-authentik-meta-app,X-authentik-meta-version"
    ]
  }
}
SVCDEF

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

# --- Bootstrap Authentik via Django ORM ---
# Using docker exec avoids API token auth issues across Authentik versions.
echo "Bootstrapping Authentik..."

cat > /tmp/ak_bootstrap.py << 'PYEOF'
from authentik.flows.models import Flow, FlowDesignation
from authentik.providers.proxy.models import ProxyProvider, ProxyMode
from authentik.core.models import Application, User
from authentik.outposts.models import Outpost

import os

# Ensure admin user has correct email and password
admin_username = os.environ.get("AUTHENTIK_BOOTSTRAP_USERNAME", "akadmin")
admin = User.objects.filter(username=admin_username).first()
if not admin:
    print(f"ERROR: Admin user '{admin_username}' not found"); exit(1)
admin.email = "raygenrrupe@gmail.com"
bootstrap_password = os.environ.get("AUTHENTIK_BOOTSTRAP_PASSWORD", "")
if bootstrap_password:
    admin.set_password(bootstrap_password)
admin.save()
print(f"Admin user: {admin.username} ({admin.email})")

flow = Flow.objects.filter(designation=FlowDesignation.AUTHENTICATION).first()
if not flow:
    print("ERROR: No authentication flow found"); exit(1)
print(f"Flow: {flow.slug}")

outpost = Outpost.objects.filter(name__icontains="embedded").first()
if not outpost:
    print("ERROR: No embedded outpost found"); exit(1)
print(f"Outpost: {outpost.name}")

protected = [
    ("Demo App",   "demo-app",   "http://demo.bagelindustries.com"),
    ("Nomad UI",   "nomad-ui",   "http://nomad.bagelindustries.com"),
    ("Consul UI",  "consul-ui",  "http://consul.bagelindustries.com"),
]

for name, slug, host in protected:
    provider, created = ProxyProvider.objects.get_or_create(
        name=f"{name} Forward Auth",
        defaults={"authorization_flow": flow, "mode": ProxyMode.FORWARD_SINGLE, "external_host": host},
    )
    if not created:
        provider.external_host = host
        provider.save()
    print(f"Provider {provider.pk}: {name} (created={created})")

    app, created = Application.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "provider": provider},
    )
    if not created:
        app.provider = provider
        app.save()
    print(f"App {app.slug} (created={created})")

    outpost.providers.add(provider)

config = outpost.config
config["authentik_host"] = "http://authentik.bagelindustries.com"
config["authentik_host_insecure"] = True
outpost.config = config
outpost.save()
print("Bootstrap complete")
PYEOF

docker cp /tmp/ak_bootstrap.py authentik-server-1:/tmp/ak_bootstrap.py
docker exec authentik-server-1 ak shell -c "exec(open('/tmp/ak_bootstrap.py').read())"
echo "Authentik bootstrap complete"
