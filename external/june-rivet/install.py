"""Install only June's loopback-facing Rivet tunnel, never the application."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "2026.9.3"
SHA256 = "77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2"
DIRECTORY = Path("/usr/local/lib/june-rivet")
CREDENTIALS = Path("/etc/june-rivet-tunnel")
UNIT = Path("/etc/systemd/system/june-rivet-tunnel.service")


def write_changed(path, content, mode):
    if path.exists() and path.read_bytes() == content:
        path.chmod(mode)
        return False
    temporary = path.with_suffix(".new")
    with temporary.open("wb") as output:
        output.write(content)
    temporary.chmod(mode)
    temporary.replace(path)
    return True


def main():
    os.umask(0o077)
    payload = json.load(sys.stdin)
    DIRECTORY.mkdir(mode=0o755, parents=True, exist_ok=True)
    DIRECTORY.chmod(0o755)
    CREDENTIALS.mkdir(mode=0o700, exist_ok=True)
    binary = DIRECTORY / "cloudflared"
    changed = False
    if not binary.exists() or hashlib.sha256(binary.read_bytes()).hexdigest() != SHA256:
        url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-linux-amd64"
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != SHA256:
            raise RuntimeError("cloudflared checksum mismatch")
        changed = write_changed(binary, content, 0o755)
    changed = write_changed(CREDENTIALS / "token", payload["token"].encode(), 0o600) or changed
    changed = write_changed(UNIT, payload["service"].encode(), 0o644) or changed
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "june-rivet-tunnel.service"], check=True)
    subprocess.run([
        "systemctl", "restart" if changed else "start", "june-rivet-tunnel.service",
    ], check=True)
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:20242/ready", timeout=2) as response:
                if response.status == 200:
                    print("June Rivet tunnel ready")
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2)
    raise RuntimeError("June Rivet tunnel did not become ready")


if __name__ == "__main__":
    main()
