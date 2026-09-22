#!/usr/bin/env python3
"""Stage the Switchboard plugin without copying Amp credentials or restarting it.

The Switchboard token travels only through captured SSH pipes. It is never part
of an argument, log message, local file, or exception shown by this installer.
All existing target files are checked for conflicts before any target is changed.
"""

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys


INSTALL = r'''
import json, os, pathlib, pwd, subprocess, sys, tempfile

def install():
    payload = json.load(sys.stdin)
    account = pwd.getpwnam("amp")
    home = pathlib.Path(account.pw_dir)
    if home != pathlib.Path("/home/amp"):
        raise ValueError("Unexpected runner account home")
    base = home / ".config/amp"
    paths = {
        base / "switchboard.token": payload["token"].encode(),
        base / "switchboard.json": (json.dumps({
            "url": "http://192.168.0.233:8088",
            "token_file": str(base / "switchboard.token"),
        }, indent=2) + "\n").encode(),
        base / "plugins/switchboard.ts": payload["plugin"].encode(),
    }
    directories = [home / ".config", base, base / "plugins"]
    conflicts = []
    current = []
    for path in directories:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            conflicts.append(str(path))
    for path, content in paths.items():
        if path.is_symlink() or (path.exists() and not path.is_file()):
            conflicts.append(str(path))
        elif path.exists():
            same = path.read_bytes() == content
            if path.name == "switchboard.json":
                try:
                    same = json.loads(path.read_text()) == json.loads(content)
                except (ValueError, UnicodeError):
                    same = False
            if same:
                current.append(str(path))
            else:
                conflicts.append(str(path))
    if conflicts:
        print(json.dumps({"ok": False, "conflicts": conflicts, "changed": False}))
        return 2
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, account.pw_uid, account.pw_gid)
        os.chmod(directory, 0o700)
    installed = []
    for path, content in paths.items():
        if str(path) not in current:
            fd, temporary = tempfile.mkstemp(prefix=".switchboard-install-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                os.chown(temporary, account.pw_uid, account.pw_gid)
                os.chmod(temporary, 0o600)
                # No existing file is replaced, even if provisioning writes one
                # after the preflight check. Hard-link creation is atomic.
                os.link(temporary, path)
                installed.append(str(path))
            finally:
                pathlib.Path(temporary).unlink(missing_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o600)
    status = subprocess.run(["systemctl", "is-active", "amp-runner"], capture_output=True, text=True)
    print(json.dumps({"ok": True, "installed": installed, "already_current": current,
        "runner_state": status.stdout.strip(),
        "runner_auth_file_present": (home / ".local/share/amp/secrets.json").is_file(),
        "restarted": False}))
    return 0

try:
    sys.exit(install())
except Exception as error:
    print(json.dumps({"ok": False, "error": type(error).__name__,
        "message": "Plugin staging failed; inspect destination paths. No service restart was requested."}))
    sys.exit(1)
'''


def ssh(args, host, known_hosts=None):
    command = ["ssh", "-F", "/dev/null", "-i", str(args.ssh_key.expanduser()),
               "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=yes"]
    if known_hosts:
        command += ["-o", "UserKnownHostsFile=" + str(known_hosts.expanduser())]
    return command + ["root@" + host]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-host", default="192.168.0.214")
    parser.add_argument("--phone-host", default="192.168.0.233")
    parser.add_argument("--ssh-key", type=Path, default=Path("~/.ssh/proxmox-pulumi"))
    parser.add_argument("--phone-known-hosts", type=Path, default=Path("~/.ssh/codex-phone-cluster-known-hosts"))
    parser.add_argument("--runner-known-hosts", type=Path)
    args = parser.parse_args()
    plugin = Path(__file__).with_name("amp_switchboard_plugin.ts").read_text()
    source = subprocess.run(
        ssh(args, args.phone_host, args.phone_known_hosts)
        + ["cat /var/lib/codex-phone/switchboard/admin-token"],
        capture_output=True, timeout=20, check=False)
    if source.returncode:
        raise RuntimeError("Could not read the private Switchboard token over pinned SSH")
    token = source.stdout.decode().strip()
    if not token or len(token) > 4096 or "\n" in token or "\r" in token:
        raise RuntimeError("The Switchboard token file is invalid")
    payload = json.dumps({"plugin": plugin, "token": token}).encode()
    result = subprocess.run(
        ssh(args, args.runner_host, args.runner_known_hosts)
        + ["python3 -c " + shlex.quote(INSTALL)],
        input=payload, capture_output=True, timeout=30, check=False)
    # Only the installer's small, explicitly non-secret JSON summary is shown.
    try:
        summary = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise RuntimeError("Runner staging returned no valid status; inspect runner connectivity") from None
    allowed = {"ok", "conflicts", "changed", "installed", "already_current",
               "runner_state", "runner_auth_file_present", "restarted", "error", "message"}
    print(json.dumps({key: value for key, value in summary.items() if key in allowed}, indent=2))
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired) as error:
        # No stderr from SSH or source-file content is included in failures.
        print("Amp plugin installer: " + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
