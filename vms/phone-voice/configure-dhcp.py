"""Move the existing single-phone DHCP helper onto the phone VM."""
import json
from pathlib import Path
import subprocess
import sys

payload = json.load(sys.stdin)
directory = Path("/etc/cisco-phone")
directory.mkdir(mode=0o755, exist_ok=True)
(directory / "dnsmasq.conf").write_text("""# Only the owner's Cisco phone receives a DHCP reply; there is no pool.
port=0
interface=eth0
except-interface=lo
bind-interfaces
keep-in-foreground
user=root
log-facility=-
log-dhcp
dhcp-leasefile=/tmp/cisco-recovery.leases
dhcp-range=192.168.0.0,static,255.255.0.0,10m
dhcp-host=0c:27:24:31:7f:2e,id:*,set:cisco_recovery,192.168.0.197,10m
dhcp-ignore=tag:!cisco_recovery
dhcp-authoritative
dhcp-option=tag:cisco_recovery,option:router,192.168.0.1
dhcp-option=tag:cisco_recovery,option:dns-server,192.168.0.1
dhcp-option=tag:cisco_recovery,28,192.168.255.255
dhcp-option-force=tag:cisco_recovery,150,192.168.0.233
dhcp-option-force=tag:cisco_recovery,66,"192.168.0.233"
dhcp-boot=tag:cisco_recovery,term45.default.loads,,192.168.0.233
dhcp-no-override
""")
Path("/etc/systemd/system/cisco-phone-dhcp.service").write_text("""[Unit]
Description=DHCP provisioning for Cisco phone 0C2724317F2E only
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/docker run --rm --name sccp-recovery-dhcp --network host --cap-add NET_ADMIN --mount type=bind,source=/etc/cisco-phone/dnsmasq.conf,target=/etc/cisco-recovery.conf,readonly sccp-tftp:latest dnsmasq --conf-file=/etc/cisco-recovery.conf
ExecStop=/usr/bin/docker stop -t 5 sccp-recovery-dhcp
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
""")
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable" if payload["enabled"] else "disable", "cisco-phone-dhcp.service"], check=True)
subprocess.run(["systemctl", "restart" if payload["enabled"] else "stop", "cisco-phone-dhcp.service"], check=True)
print("Single-phone DHCP enabled" if payload["enabled"] else "Single-phone DHCP staged but disabled")
