#!/bin/sh
set -eu

# This state outlives Nomad allocations and is deliberately retained on deletion.
install -d -o 1000 -g 1000 -m 0700 /opt/nomad/volumes/bambu_api_data
install -d -o root -g 1000 -m 0750 /etc/bambu-api

staged=$(mktemp)
backup=$(mktemp)
trap 'rm -f "$staged" "$backup"' EXIT
cat > "$staged" <<'HCL'
client {
  host_volume "bambu_api_data" {
    path      = "/opt/nomad/volumes/bambu_api_data"
    read_only = false
  }
  host_volume "bambu_api_secrets" {
    path      = "/etc/bambu-api"
    read_only = true
  }
}
HCL

target=/etc/nomad.d/bambu-api.hcl
if [ -f "$target" ] && cmp -s "$staged" "$target"; then
    echo "bambu-api host volumes already configured"
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
echo "bambu-api persistent data and read-only secret volumes configured"
