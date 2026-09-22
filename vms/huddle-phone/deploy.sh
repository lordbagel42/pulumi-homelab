#!/bin/bash
set -euo pipefail
: "${HUDDLE_RELEASE:?Release directory is required}"
: "${HUDDLE_IP:?VM address is required}"
cd "$HUDDLE_RELEASE"

# Change only this VM's copy of the phone provisioning files. The original
# workstation PBX and TFTP server remain untouched until handset cutover.
python3 - <<'PY'
import ipaddress
import os
from pathlib import Path
import xml.etree.ElementTree as ET

address = str(ipaddress.IPv4Address(os.environ['HUDDLE_IP']))
for path in Path('tftp/files').glob('*.cnf.xml'):
    tree = ET.parse(path)
    for member in tree.findall('.//callManager/processNodeName'):
        member.text = address
    tree.write(path, encoding='UTF-8', xml_declaration=True)
PY

python3 huddle-phone/configure.py
compose=(docker compose --project-name huddle-phone -f docker-compose.yml -f docker-compose.huddle.yml)
"${compose[@]}" config --quiet
"${compose[@]}" build pbx tftp huddle-phone
# Exercise the bundled SDK constructor and audio bindings before replacing any
# running service. This is offline and never receives Slack credentials.
docker run --rm --network none --read-only \
    --tmpfs /tmp:mode=1777 --tmpfs /home/bridge:uid=10001,gid=10001,mode=700 \
    --cap-drop ALL --security-opt no-new-privileges:true --shm-size 512m \
    --mount "type=bind,src=$PWD/huddle-phone/tests,dst=/tests,readonly" \
    --entrypoint python -e PYTHONPATH=/app huddle-phone:local /tests/browser_smoke.py
if [ -f /opt/phone-platform/current/deploy/assert-idle.py ]; then
    python3 /opt/phone-platform/current/deploy/assert-idle.py
fi
"${compose[@]}" up -d pbx tftp

# AMI must be ready before checking the credentials and starting the watcher.
for attempt in $(seq 1 60); do
    if python3 -c 'import socket; socket.create_connection(("127.0.0.1", 5038), 2).close()' 2>/dev/null; then
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        echo 'Asterisk AMI did not become ready' >&2
        exit 1
    fi
    sleep 2
done
# A changed secret rewrites the bind-mounted manager.conf without changing the
# container definition. Reload it before doctor authenticates with the new key.
docker exec sccp-pbx asterisk -rx 'manager reload'
"${compose[@]}" run --rm -T huddle-phone doctor </dev/null
if [ -f /opt/phone-platform/current/deploy/assert-idle.py ]; then
    python3 /opt/phone-platform/current/deploy/assert-idle.py
fi
"${compose[@]}" up -d huddle-phone

for attempt in $(seq 1 60); do
    if curl --silent --fail --max-time 3 http://127.0.0.1:8099/healthz; then
        printf '\n'
        ln -sfn "$HUDDLE_RELEASE" /opt/huddle-phone/current
        if [ -f /opt/phone-platform/current/deploy/configure-phone.py ]; then
            python3 /opt/phone-platform/current/deploy/configure-phone.py
        fi
        exit 0
    fi
    sleep 2
done
echo 'Huddle bridge did not become healthy; inspect docker compose logs' >&2
exit 1
