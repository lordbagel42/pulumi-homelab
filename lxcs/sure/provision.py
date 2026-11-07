import json
import os
import subprocess
from pathlib import Path


def main(payload):
    os.umask(0o077)
    root = Path("/opt/sure")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # JSON string quoting is also valid Compose env-file quoting. Generated
    # passwords are hex; escape dollar signs so Compose never interpolates keys.
    (root / ".env").write_text(
        "".join(
            f"{key}={json.dumps(value).replace('$', '$$')}\n"
            for key, value in payload["env"].items()
        )
    )
    (root / ".env").chmod(0o600)
    (root / "compose.yaml").write_text(payload["compose"])
    subprocess.run(["systemctl", "enable", "--now", "docker"], check=True)
    subprocess.run(["docker", "compose", "config", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "600"],
        cwd=root,
        check=True,
    )
