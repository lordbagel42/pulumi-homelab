import json
import os
import subprocess
from pathlib import Path


def main(payload):
    os.umask(0o077)
    root = Path("/opt/cpa-manager")
    for directory in (
        root,
        root / "secrets",
        root / "data",
        root / "cpa",
        root / "cpa/auths",
        root / "cpa/logs",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for filename, key in (
        ("admin-key", "adminKey"),
        ("data-key", "dataKey"),
        ("upstream-key", "upstreamKey"),
    ):
        (root / "secrets" / filename).write_text(payload[key] + "\n")
    config = root / "cpa/config.yaml"
    # Preserve provider/account settings subsequently saved through the UI.
    if not config.exists():
        config.write_text(
            json.dumps(
                {
                    "host": "0.0.0.0",
                    "port": 8317,
                    "auth-dir": "/root/.cli-proxy-api",
                    "api-keys": [payload["clientKey"]],
                    "remote-management": {
                        "allow-remote": True,
                        "secret-key": payload["upstreamKey"],
                        "disable-control-panel": True,
                    },
                    "usage-statistics-enabled": True,
                    "logging-to-file": False,
                    "request-log": False,
                    "logs-max-total-size-mb": 20,
                },
                indent=2,
            )
            + "\n"
        )
    (root / "compose.yaml").write_text(payload["compose"])
    journal = Path("/etc/systemd/journald.conf.d")
    journal.mkdir(exist_ok=True)
    (journal / "cpa-manager.conf").write_text(
        "[Journal]\nSystemMaxUse=32M\nRuntimeMaxUse=16M\nMaxRetentionSec=7day\n"
    )
    subprocess.run(["systemctl", "restart", "systemd-journald"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "docker"], check=True)
    subprocess.run(["docker", "compose", "config", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "180"],
        cwd=root,
        check=True,
    )
