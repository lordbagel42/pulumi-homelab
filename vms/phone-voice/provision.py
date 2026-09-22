"""Provision VM 233's voice services. Private state is imported only at cutover."""
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
DATA_HOME = Path("/var/lib/codex-phone")


def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def write(path, text, mode=0o644, owner=None):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(text.encode())
        os.fchmod(staged.fileno(), mode)
        if owner:
            os.fchown(staged.fileno(), owner.pw_uid, owner.pw_gid)
    os.replace(staged.name, path)


def main(payload):
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    release = Path(payload["release"])
    if not re.fullmatch(r"/opt/phone-voice/releases/[0-9a-f]{64}/source", str(release)):
        raise ValueError("Unexpected release directory")
    tunnel_key = payload["tunnelKey"].strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?", tunnel_key):
        raise ValueError("Invalid tunnel public key")
    config = json.loads(payload["homeAssistant"])
    if not config.get("url") or not config.get("token"):
        raise ValueError("Home Assistant URL and token are required")
    run("apt-get", "update", "-qq")
    run("apt-get", "install", "-y", "--no-install-recommends", "python3-venv", "libgomp1",
        "libespeak-ng1", "ca-certificates", "git", "ripgrep", "curl")
    try:
        account = pwd.getpwnam("codex-phone")
    except KeyError:
        run("useradd", "--create-home", "--home-dir", str(DATA_HOME), "--shell", "/bin/bash", "codex-phone")
        account = pwd.getpwnam("codex-phone")
    run("usermod", "-aG", "docker", "codex-phone")
    for folder in (DATA_HOME, DATA_HOME / "state", DATA_HOME / "models", DATA_HOME / ".codex", DATA_HOME / "workspace", DATA_HOME / ".ssh"):
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        os.chown(folder, account.pw_uid, account.pw_gid)
    # Amp credentials are created by a separate device login as codex-phone;
    # never copy the workstation account's login or put it in Pulumi state.
    amp_binary = DATA_HOME / ".amp/bin/amp"
    if not amp_binary.is_file():
        installer = DATA_HOME / "state/amp-install.sh"
        with urllib.request.urlopen("https://ampcode.com/install.sh", timeout=60) as response:
            script = response.read(1024 * 1024 + 1)
        if len(script) > 1024 * 1024:
            raise ValueError("Unexpected Amp installer size")
        write(installer, script.decode(), 0o700, account)
        run("runuser", "-u", "codex-phone", "--", "/bin/bash", str(installer))
        if not amp_binary.is_file():
            raise ValueError("Amp installation did not produce its CLI")
    write(DATA_HOME / "home-assistant.json", json.dumps(config) + "\n", 0o600, account)
    operator_secret = payload.get("operatorSecret", "")
    operator_config = DATA_HOME / "state/huddle-operator.json"
    if operator_secret:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", operator_secret):
            raise ValueError("Invalid huddle operator secret")
        write(operator_config, json.dumps({"secret": operator_secret, "http_port": 8099,
              "audio_port": 9094, "owner_id": payload.get("operatorOwnerId")}) + "\n", 0o600, account)
    else:
        operator_config.unlink(missing_ok=True)
    write(DATA_HOME / ".ssh/authorized_keys", "restrict,port-forwarding " + tunnel_key + "\n", 0o600, account)
    write("/etc/ssh/sshd_config.d/60-codex-phone.conf", """Match User codex-phone
    AllowTcpForwarding yes
    AllowStreamLocalForwarding yes
    StreamLocalBindUnlink yes
    StreamLocalBindMask 0177
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/bin/false
Match all
""")
    run("sshd", "-t")
    run("systemctl", "reload", "ssh")
    package_root = Path("/opt/codex") / CODEX_VERSION
    if not (package_root / "codex-package.json").exists():
        archive = Path(f"/var/cache/codex-{CODEX_VERSION}-linux-x64.tgz")
        if not archive.exists():
            with urllib.request.urlopen(CODEX_URL, timeout=120) as response, archive.with_suffix(".new").open("wb") as output:
                shutil.copyfileobj(response, output)
            archive.with_suffix(".new").replace(archive)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha512").digest() != base64.b64decode(CODEX_SHA512):
                raise ValueError("Codex package checksum mismatch")
        prefix = "package/vendor/x86_64-unknown-linux-musl/"
        # Keep the official layout: shell tools require the code-mode host,
        # and package metadata locates bundled ripgrep, zsh and bwrap.
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                if not member.isfile() or not member.name.startswith(prefix):
                    continue
                relative = Path(member.name.removeprefix(prefix))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Unexpected Codex package path")
                target = package_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for name in ("codex", "codex-code-mode-host"):
        link = Path("/usr/local/bin") / name
        staged = link.with_suffix(".new")
        staged.unlink(missing_ok=True)
        staged.symlink_to(package_root / "bin" / name)
        staged.replace(link)
    # Debian 12's Python 3.11 includes audioop and supports the pinned wheels.
    venv = Path("/opt/phone-voice/venv")
    if not (venv / "bin/python").exists():
        run("python3", "-m", "venv", str(venv))
    run(str(venv / "bin/pip"), "install", "--disable-pip-version-check", "-r", str(release / "requirements.txt"))
    for name in ("state", "models"):
        link = release / name
        if not link.is_symlink():
            link.symlink_to(DATA_HOME / name, target_is_directory=True)
    current = Path("/opt/phone-voice/current")
    staged = current.with_name("current.new")
    staged.unlink(missing_ok=True)
    staged.symlink_to(release, target_is_directory=True)
    staged.replace(current)
    config_path = DATA_HOME / ".codex/config.toml"
    config_text = config_path.read_text() if config_path.exists() else ""
    if "[mcp_servers.codex_phone]" not in config_text:
        config_text += '\n[mcp_servers.codex_phone]\ncommand = "/opt/phone-voice/venv/bin/python"\nargs = ["/opt/phone-voice/current/phone_mcp.py"]\ntool_timeout_sec = 150\n'
        write(config_path, config_text, 0o600, account)
    write("/etc/systemd/system/codex-phone-bridge.service", payload["codexService"])
    write("/etc/systemd/system/home-assistant-phone.service", payload["assistService"])
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "codex-phone-bridge.service", "home-assistant-phone.service")
    # First provisioning intentionally waits for the one-time state migration.
    if (DATA_HOME / "migration-complete").exists():
        idle_guard = Path("/opt/phone-platform/current/deploy/assert-idle.py")
        if idle_guard.is_file():
            run("python3", str(idle_guard))
        run("systemctl", "restart", "codex-phone-bridge.service", "home-assistant-phone.service")
    print("Voice services provisioned; persistent state remains outside release directories.")


if __name__ == "__main__":
    main(json.load(sys.stdin))
