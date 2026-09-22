"""Install the bridge without putting its bearer token in command arguments."""

import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

CODEX_VERSION = "0.154.0"
CODEX_URL = "https://registry.npmjs.org/@openai/codex/-/codex-0.154.0-linux-x64.tgz"
CODEX_SHA512 = "a4FI3A8sGtwGrOqltrPbrS2hajrHQG591EwmRfiRoLMb10VxdBtUGW4gu6IJVYENiYGA7k3P4jlRHEoCZU/s9Q=="


def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def write(path, content, mode=0o644, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(content.encode())
        os.fchmod(staged.fileno(), mode)
        if owner:
            os.fchown(staged.fileno(), owner.pw_uid, owner.pw_gid)
    os.replace(staged.name, path)


def main(payload):
    os.umask(0o077)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", payload["token"]):
        raise ValueError("Invalid bridge token")

    run("apt-get", "update", "-qq")
    run("apt-get", "install", "-y", "--no-install-recommends", "ca-certificates")
    try:
        account = pwd.getpwnam("chatgpt-bridge")
    except KeyError:
        run("useradd", "--system", "--create-home", "--home-dir", "/var/lib/chatgpt-bridge",
            "--shell", "/usr/sbin/nologin", "chatgpt-bridge")
        account = pwd.getpwnam("chatgpt-bridge")
    os.chmod(account.pw_dir, 0o700)
    codex_dir = Path(account.pw_dir) / ".codex"
    codex_dir.mkdir(mode=0o700, exist_ok=True)
    os.chown(codex_dir, account.pw_uid, account.pw_gid)
    os.chmod(codex_dir, 0o700)

    codex = Path("/usr/local/bin/codex")
    installed = subprocess.run([str(codex), "--version"], capture_output=True, text=True).stdout.strip() if codex.exists() else ""
    if installed != f"codex-cli {CODEX_VERSION}":
        with tempfile.TemporaryDirectory(prefix="chatgpt-install-") as folder:
            archive = Path(folder) / "codex.tgz"
            digest = hashlib.sha512()
            with urllib.request.urlopen(CODEX_URL, timeout=120) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.digest() != base64.b64decode(CODEX_SHA512):
                raise ValueError("Codex package checksum did not match")
            with tarfile.open(archive) as package:
                members = [entry for entry in package.getmembers() if entry.isfile() and entry.name == "package/vendor/x86_64-unknown-linux-musl/bin/codex"]
                if len(members) != 1:
                    raise ValueError("Unexpected Codex package layout")
                with package.extractfile(members[0]) as source, codex.with_suffix(".new").open("wb") as output:
                    shutil.copyfileobj(source, output)
            os.chmod(codex.with_suffix(".new"), 0o755)
            os.replace(codex.with_suffix(".new"), codex)
    run(str(codex), "--version")

    Path("/opt/chatgpt-bridge").mkdir(mode=0o755, exist_ok=True)
    os.chmod("/opt/chatgpt-bridge", 0o755)
    write("/opt/chatgpt-bridge/chatgpt_gateway.py", payload["gateway"])
    write("/var/lib/chatgpt-bridge/token", payload["token"] + "\n", mode=0o600, owner=account)
    write("/etc/systemd/system/home-assistant-chatgpt.service", payload["service"])
    run("python3", "-m", "py_compile", "/opt/chatgpt-bridge/chatgpt_gateway.py")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "home-assistant-chatgpt.service")
    run("systemctl", "restart", "home-assistant-chatgpt.service")
    run("systemctl", "is-active", "home-assistant-chatgpt.service")


if __name__ == "__main__":
    main(json.load(sys.stdin))
