#!/usr/bin/env bash
# Pulumi sends the non-secret portal config on stdin; the session signing key
# remains on the runner and never appears in Pulumi output or child environments.
set -euo pipefail
umask 077

archive=/opt/raygen-portals/release.tgz
release=/opt/raygen-portals/releases/__RELEASE_SHA__
printf '%s  %s\n' '__RELEASE_SHA__' "$archive" | sha256sum --check --status
install -d -m 0755 /opt/raygen-portals /opt/raygen-portals/releases "$release"
tar -xzf "$archive" -C "$release" --no-same-owner
chmod 0644 "$release/daemon.mjs" "$release/release.json"
chmod 0755 "$release/cli.mjs"
install -d -m 0700 -o amp -g amp /var/lib/raygen-portals
install -d -m 0750 -o root -g amp /etc/raygen-portals

config=$(mktemp)
trap 'rm -f "$config"' EXIT
cat > "$config"
python3 - "$config" <<'PY'
import json, os, pathlib, secrets
source = pathlib.Path(__import__('sys').argv[1])
config = json.loads(source.read_text())
key = pathlib.Path('/var/lib/raygen-portals/signing-key')
if not key.exists():
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secrets.token_hex(32))
config['signingKey'] = key.read_text().strip()
target = pathlib.Path('/etc/raygen-portals/config.json')
temporary = target.with_suffix('.tmp')
temporary.write_text(json.dumps(config))
temporary.chmod(0o640)
temporary.replace(target)
PY
chown amp:amp /var/lib/raygen-portals/signing-key
chown root:amp /etc/raygen-portals/config.json

ln -sfn "$release" /opt/raygen-portals/current
ln -sfn /opt/raygen-portals/current/cli.mjs /usr/local/bin/raygen-portal
install -d -m 0700 -o amp -g amp /home/amp/.config/amp/plugins
install -m 0644 -o amp -g amp "$release/raygen-portals.js" /home/amp/.config/amp/plugins/raygen-portals.js

cat > /etc/systemd/system/raygen-portals.service <<'UNIT'
[Unit]
Description=Private Raygen preview portals
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=amp
Group=amp
WorkingDirectory=/home/amp/workspaces
Environment=HOME=/home/amp
Environment=PATH=/home/amp/.local/share/pnpm:/home/amp/.amp/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/node /opt/raygen-portals/current/daemon.mjs /etc/raygen-portals/config.json
RuntimeDirectory=raygen-portals
RuntimeDirectoryMode=0700
StateDirectory=raygen-portals
StateDirectoryMode=0700
UMask=0077
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
RestrictSUIDSGID=true
MemoryHigh=1G
MemoryMax=2G
CPUQuota=100%
TasksMax=512

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable raygen-portals.service
systemctl restart raygen-portals.service
for _ in $(seq 1 30); do
  if /usr/local/bin/raygen-portal list --thread T-00000000-0000-0000-0000-000000000000 >/dev/null 2>&1; then
    systemctl is-active --quiet raygen-portals.service
    echo 'Raygen portals is ready.'
    exit 0
  fi
  sleep 1
done
echo 'Raygen portals did not become ready; inspect its systemd journal.' >&2
exit 1
