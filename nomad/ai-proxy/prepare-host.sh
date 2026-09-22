#!/bin/sh
set -eu

# This state outlives Nomad allocations and is deliberately retained on deletion.
install -d -o 1000 -g 1000 -m 0700 /opt/nomad/volumes/ai_proxy_data
install -d -o root -g 1000 -m 0750 /etc/ai-proxy

staged=$(mktemp)
backup=$(mktemp)
trap 'rm -f "$staged" "$backup"' EXIT
cat > "$staged" <<'HCL'
client {
  max_kill_timeout = "45s"
  host_volume "ai_proxy_data" {
    path      = "/opt/nomad/volumes/ai_proxy_data"
    read_only = false
  }
  host_volume "ai_proxy_secrets" {
    path      = "/etc/ai-proxy"
    read_only = true
  }
}
HCL

target=/etc/nomad.d/ai-proxy.hcl
if [ -f "$target" ] && cmp -s "$staged" "$target"; then
    echo "ai-proxy host volumes already configured"
    exit 0
fi

had_config=false
if [ -f "$target" ]; then
    cp "$target" "$backup"
    had_config=true
fi
restore_config() {
    if [ "$had_config" = true ]; then
        install -m 0644 "$backup" "$target"
    else
        rm -f "$target"
    fi
}
install -m 0644 "$staged" "$target"
if ! nomad config validate /etc/nomad.d; then
    restore_config
    exit 1
fi
if ! systemctl restart nomad; then
    restore_config
    systemctl restart nomad
    exit 1
fi

attempt=0
until curl --fail --silent http://127.0.0.1:4646/v1/agent/health >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        restore_config
        systemctl restart nomad
        echo "Nomad did not become healthy after volume registration" >&2
        exit 1
    fi
    sleep 2
done
echo "ai-proxy persistent data and read-only secret volumes configured"
