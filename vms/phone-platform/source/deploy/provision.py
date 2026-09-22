"""Install Switchboard on the existing phone VM; never restart the voice bridge."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile

STATE = Path("/var/lib/codex-phone/switchboard")


def run(*arguments):
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL)


def write(path, content, mode=0o644, owner=None):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(content.encode())
        os.fchmod(staged.fileno(), mode)
        if owner:
            os.fchown(staged.fileno(), owner.pw_uid, owner.pw_gid)
    os.replace(staged.name, path)


def main(payload):
    release = Path(payload["release"])
    if not re.fullmatch(r"/opt/phone-platform/releases/[0-9a-f]{64}/source", str(release)):
        raise ValueError("Unexpected platform release directory")
    if not (release / "phone_platform/__main__.py").is_file():
        raise ValueError("Platform entry point is missing")
    token = payload["adminToken"].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
        raise ValueError("Invalid platform admin token")
    phone_token = payload["phoneToken"].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", phone_token):
        raise ValueError("Invalid platform phone-directory token")
    account = pwd.getpwnam("codex-phone")
    for folder in (STATE, STATE / "asterisk"):
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        os.chown(folder, account.pw_uid, account.pw_gid)
    write(STATE / "admin-token", token + "\n", 0o600, account)
    write(STATE / "phone-token", phone_token + "\n", 0o600, account)
    routes = STATE / "asterisk/routes.conf"
    if not routes.exists():
        write(routes, "[switchboard-routes]\n", 0o644, account)
    secret = payload.get("huddleSecret", "")
    if secret:
        write(STATE / "huddle.json", json.dumps({
            "secret": secret,
            "http_port": 8099,
            "audio_port": 9094,
            "owner_id": payload.get("huddleOwnerId", ""),
        }) + "\n", 0o600, account)

    venv = Path("/opt/phone-platform/venv")
    if not (venv / "bin/python").exists():
        run("python3", "-m", "venv", str(venv))
    # Keep all new server packages in this venv. Reuse the already pinned,
    # heavyweight speech wheels without changing the active voice environment.
    voice_python = "/opt/phone-voice/venv/bin/python"
    site_query = "import sysconfig; print(sysconfig.get_path('purelib'))"
    voice_site = subprocess.check_output([voice_python, "-c", site_query], text=True).strip()
    platform_site = subprocess.check_output([str(venv / "bin/python"), "-c", site_query], text=True).strip()
    write(Path(platform_site) / "phone-voice.pth", voice_site + "\n")
    run(str(venv / "bin/pip"), "install", "--disable-pip-version-check", "-r", str(release / "requirements.txt"))
    run(str(venv / "bin/python"), "-c", "import fastapi, uvicorn, faster_whisper, piper")

    run("python3", str(release / "deploy/assert-idle.py"))

    current = Path("/opt/phone-platform/current")
    staged = current.with_name("current.new")
    staged.unlink(missing_ok=True)
    staged.symlink_to(release, target_is_directory=True)
    staged.replace(current)
    write("/etc/systemd/system/phone-platform.service", payload["service"])
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "phone-platform.service")
    run("systemctl", "restart", "phone-platform.service")
    run("python3", str(release / "deploy/configure-phone.py"))
    print("Phone platform installed; existing voice services were not restarted.")


if __name__ == "__main__":
    main(json.load(sys.stdin))
