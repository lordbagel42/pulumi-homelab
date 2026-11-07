"""Install the outbound-only E2B controller; secrets arrive only over stdin."""

import gzip
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(content.encode())
        os.fchmod(staged.fileno(), mode)
    os.replace(staged.name, path)


def main(payload):
    os.umask(0o077)
    for value in (
        payload["apiKey"],
        payload["providerToken"],
        payload["template"],
        payload["rootPassword"],
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]*", value):
            raise ValueError("Invalid credential or template format")
    if not payload["apiKey"] or not payload["template"]:
        raise ValueError("E2B key and template are required")
    if payload["providerToken"] and not payload["providerToken"].startswith(
        "aop_live_"
    ):
        raise ValueError("Expected an Amp orb provider token")

    subprocess.run(
        ["chpasswd"],
        input=("root:" + payload["rootPassword"] + "\n").encode(),
        check=True,
    )
    write(
        "/etc/ssh/sshd_config.d/10-amp-orbs.conf",
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n",
    )
    run("sshd", "-t")
    # Debian 13's socket-activated sshd cannot rebind its inherited socket on HUP.
    run("systemctl", "restart", "ssh")
    run("apt-get", "update", "-qq")
    run(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "ca-certificates",
        "xz-utils",
    )
    # E2B 2.58 requires Node >=22.19; Debian's Node 20 is too old.
    node_version = "v22.23.4"
    node_archive = f"node-{node_version}-linux-x64.tar.xz"
    node_base = f"https://nodejs.org/dist/{node_version}"
    installed = (
        subprocess.run(
            ["/usr/local/bin/node", "--version"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path("/usr/local/bin/node").exists()
        else ""
    )
    if installed != node_version:
        sums = (
            urllib.request.urlopen(node_base + "/SHASUMS256.txt", timeout=60)
            .read()
            .decode()
        )
        expected = next(
            line.split()[0]
            for line in sums.splitlines()
            if line.split()[-1] == node_archive
        )
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / node_archive
            urllib.request.urlretrieve(node_base + "/" + node_archive, archive)
            if (
                hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
                != expected
            ):
                raise ValueError("Node checksum mismatch")
            with tarfile.open(archive) as package:
                package.extractall(folder, filter="data")
            run(
                "cp",
                "-a",
                str(Path(folder) / f"node-{node_version}-linux-x64") + "/.",
                "/usr/local/",
            )
    # Tar-created intermediate directories inherit our restrictive umask.
    # /usr/local contains only public toolchains, never service credentials.
    run("chmod", "-R", "a+rX", "/usr/local")
    try:
        account = pwd.getpwnam("amp-orbs")
    except KeyError:
        run(
            "useradd",
            "--system",
            "--create-home",
            "--home-dir",
            "/var/lib/amp-orbs",
            "--shell",
            "/usr/sbin/nologin",
            "amp-orbs",
        )
        account = pwd.getpwnam("amp-orbs")
    os.chmod(account.pw_dir, 0o700)

    # Follow the official installer's versioned download and checksum contract.
    base = "https://static.ampcode.com/cli"
    version = (
        urllib.request.urlopen(base + "/cli-version.txt", timeout=60)
        .read()
        .decode()
        .strip()
    )
    binary = Path("/usr/local/bin/amp")
    marker = Path("/opt/amp-orbs/amp-version")
    if (
        not binary.exists()
        or not marker.exists()
        or marker.read_text().strip() != version
    ):
        checksum = (
            urllib.request.urlopen(f"{base}/{version}/linux-x64-amp.sha256", timeout=60)
            .read()
            .decode()
            .split()[0]
        )
        with tempfile.NamedTemporaryFile(dir=binary.parent, delete=False) as staged:
            digest = hashlib.sha256()
            with (
                urllib.request.urlopen(
                    f"{base}/{version}/amp-linux-x64.gz", timeout=120
                ) as response,
                gzip.GzipFile(fileobj=response) as source,
            ):
                while chunk := source.read(1024 * 1024):
                    staged.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != checksum:
                os.unlink(staged.name)
                raise ValueError("Amp binary checksum mismatch")
            os.fchmod(staged.fileno(), 0o755)
        os.replace(staged.name, binary)
        write(marker, version + "\n")

    Path("/opt/amp-orbs").mkdir(mode=0o755, exist_ok=True)
    os.chmod("/opt/amp-orbs", 0o755)
    for name in ("package.json", "pnpm-lock.yaml", "dispatch.mjs"):
        write(
            "/opt/amp-orbs/" + name,
            payload["files"][name],
            0o755 if name == "dispatch.mjs" else 0o644,
        )
    run("npm", "install", "--global", "pnpm@10.33.0", "--ignore-scripts")
    run(
        "pnpm",
        "--dir",
        "/opt/amp-orbs",
        "install",
        "--prod",
        "--frozen-lockfile",
        "--ignore-scripts",
    )
    # Root installs dependencies; only state/cache are writable by the service.
    run("chmod", "-R", "a+rX", "/opt/amp-orbs/node_modules")
    Path("/etc/amp-orbs").mkdir(mode=0o700, exist_ok=True)
    write(
        "/etc/amp-orbs/e2b.env",
        f"E2B_API_KEY={payload['apiKey']}\nE2B_TEMPLATE={payload['template']}\n",
        0o600,
    )
    provider = Path("/etc/amp-orbs/provider.env")
    if payload["providerToken"]:
        write(provider, f"AMP_ORB_PROVIDER_TOKEN={payload['providerToken']}\n", 0o600)
    else:
        provider.unlink(missing_ok=True)
    settings = Path(account.pw_dir) / ".config/amp/settings.json"
    write(settings, json.dumps({"amp.updates.mode": "disabled"}) + "\n", 0o600)
    run("chown", "-R", "amp-orbs:amp-orbs", account.pw_dir)
    write("/etc/systemd/system/amp-orbs.service", payload["files"]["amp-orbs.service"])
    write(
        "/etc/systemd/journald.conf.d/amp-orbs.conf",
        "[Journal]\nStorage=persistent\nSystemMaxUse=32M\nMaxRetentionSec=14day\n",
    )
    run("/usr/local/bin/node", "--check", "/opt/amp-orbs/dispatch.mjs")
    run(
        "runuser",
        "-u",
        "amp-orbs",
        "--",
        "/usr/local/bin/node",
        "-e",
        "require('/opt/amp-orbs/node_modules/e2b')",
    )
    run("runuser", "-u", "amp-orbs", "--", "/usr/local/bin/amp", "--version")
    run("systemd-analyze", "verify", "/etc/systemd/system/amp-orbs.service")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "amp-orbs.service")
    run("systemctl", "restart", "amp-orbs.service")
    if payload["providerToken"]:
        run("systemctl", "is-active", "amp-orbs.service")
    else:
        print(
            "Controller installed; activation awaits AMP_ORB_PROVIDER_TOKEN in Infisical /amp-orbs."
        )


if __name__ == "__main__":
    main(json.load(sys.stdin))
