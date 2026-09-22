#!/usr/bin/env python3
"""Stage phone data on VM 233, or copy a final snapshot with local services stopped.

This is a one-time migration, not a Pulumi update hook. It never deletes the
workstation copy. SSH uses the host key already enrolled through Proxmox.
"""
import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile

HOST = "192.168.0.233"
DESTINATION = "/var/lib/codex-phone"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--final", action="store_true", help="Require idle services, stop them, and migrate their final state")
    args = parser.parse_args()
    project = args.project.resolve()
    os.umask(0o077)
    key = subprocess.check_output(["pulumi", "config", "get", "huddle-phone:sshHostKey"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="phone-migration-") as directory:
        known = Path(directory) / "known_hosts"
        known.write_text(HOST + " " + key + "\n")
        ssh = ["ssh", "-F", "/dev/null", "-i", str(Path.home() / ".ssh/proxmox-pulumi"),
               "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
               "-o", "ConnectTimeout=10", "root@" + HOST]

        def remote(command, **kwargs):
            return subprocess.run(ssh + [command], check=True, **kwargs)

        remote("test ! -e /var/lib/codex-phone/migration-complete && id codex-phone", stdout=subprocess.DEVNULL)
        stopped = False
        try:
            if args.final:
                channels = subprocess.check_output(["docker", "exec", "sccp-pbx", "asterisk", "-rx", "core show channels concise"], text=True)
                if any(line.startswith("SCCP/") for line in channels.splitlines()):
                    raise RuntimeError("The handset is in a call; wait for hangup before cutover")
                with sqlite3.connect(project / "voice/state/sessions.sqlite3") as db:
                    if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','queued') LIMIT 1").fetchone():
                        raise RuntimeError("A phone task is active; let it finish before migration")
                subprocess.run(["systemctl", "--user", "stop", "codex-phone-bridge.service", "home-assistant-phone.service"], check=True)
                stopped = True
                remote("systemctl stop codex-phone-bridge.service home-assistant-phone.service")
            else:
                # Large, non-secret assets are staged before the brief cutover.
                producer = subprocess.Popen(["tar", "-C", str(project / "voice"), "-cf", "-", "models"], stdout=subprocess.PIPE)
                try:
                    remote("tar -C /var/lib/codex-phone -xf -", stdin=producer.stdout)
                finally:
                    producer.stdout.close()
                if producer.wait():
                    raise RuntimeError("Speech model copy failed")
                exclude = {".venv", "node_modules", ".git", ".env", "state", "models", "generated", "__pycache__"}

                def public_source(info):
                    if any(part in exclude for part in Path(info.name).parts) or info.name.endswith((".pyc", "home-assistant.json")):
                        return None
                    if info.issym() or info.islnk():
                        return None
                    return info

                process = subprocess.Popen(ssh + ["tar -C /var/lib/codex-phone -xf -"], stdin=subprocess.PIPE)
                with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                    archive.add(project, arcname="workspace", filter=public_source)
                process.stdin.close()
                if process.wait():
                    raise RuntimeError("Workspace copy failed")

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = project / "voice/state/backups" / ("proxmox-final-" if args.final else "proxmox-stage-")
            backup = backup.with_name(backup.name + stamp)
            backup.mkdir(mode=0o700, parents=True)
            snapshot = backup / "sessions.sqlite3"
            with sqlite3.connect(project / "voice/state/sessions.sqlite3") as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            snapshot.chmod(0o600)
            # Keep the backup unchanged; remap only the copy sent to the VM.
            migrated = Path(directory) / "sessions.sqlite3"
            with sqlite3.connect(snapshot) as source, sqlite3.connect(migrated) as target:
                source.backup(target)
                rows = target.execute("SELECT id,cwd FROM sessions WHERE kind='managed'").fetchall()
                for number, cwd in rows:
                    relative = Path(cwd).relative_to(project)
                    target.execute("UPDATE sessions SET cwd=? WHERE id=?", (str(Path(DESTINATION) / "workspace" / relative), number))
                threads = {row[0] for row in target.execute("SELECT thread_id FROM sessions WHERE kind='managed' AND thread_id IS NOT NULL")}
            codex_home = Path.home() / ".codex"
            histories = [file for file in (codex_home / "sessions").rglob("*.jsonl") if any(thread in file.name for thread in threads)]
            if len(histories) != len(threads):
                raise RuntimeError("A managed Codex history is missing; do not cut over")
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                archive.add(migrated, arcname="state/sessions.sqlite3")
                archive.add(codex_home / "auth.json", arcname=".codex/auth.json")
                archive.add(codex_home / "AGENTS.md", arcname=".codex/AGENTS.md")
                for file in histories:
                    archive.add(file, arcname=str(Path(".codex") / file.relative_to(codex_home)))
                config = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "low"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\nweb_search = "live"\n[agents]\nenabled = true\n[projects."/var/lib/codex-phone/workspace"]\ntrust_level = "trusted"\n'
                info = tarfile.TarInfo(".codex/config.toml")
                info.size, info.mode = len(config), 0o600
                archive.addfile(info, io.BytesIO(config))
            remote("tar -C /var/lib/codex-phone -xf - && chown -R codex-phone:codex-phone /var/lib/codex-phone && chmod 0700 /var/lib/codex-phone /var/lib/codex-phone/.codex /var/lib/codex-phone/state && chmod 0600 /var/lib/codex-phone/.codex/auth.json /var/lib/codex-phone/state/sessions.sqlite3", input=payload.getvalue())
            if args.final:
                remote("touch /var/lib/codex-phone/activated && systemctl start codex-phone-bridge.service home-assistant-phone.service")
            print(f"{'Final' if args.final else 'Staged'} copy: {len(rows)} managed sessions, {len(histories)} Codex histories. Backup: {backup}")
            if args.final:
                print("Local voice services are stopped. Validate the VM, enable the workstation SSH tunnel, then move the handset.")
        except BaseException:
            if stopped:
                subprocess.run(["systemctl", "--user", "start", "codex-phone-bridge.service", "home-assistant-phone.service"], check=False)
            raise


if __name__ == "__main__":
    main()
