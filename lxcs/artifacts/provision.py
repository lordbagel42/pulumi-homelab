"""Provision over verified SSH; secret JSON arrives on stdin, never argv."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile


def run(*args, **kwargs):
    # Suppress child output: bootstrap must not accidentally log credentials.
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def write(path, content, mode=0o600, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(content.encode())
        os.fchmod(staged.fileno(), mode)
        if owner:
            os.fchown(staged.fileno(), owner.pw_uid, owner.pw_gid)
    os.replace(staged.name, path)


def app_environment(payload):
    return {"ARTIFACTS_ORIGIN": "https://artifacts.raygen.dev",
            "ARTIFACTS_HOST_SUFFIX": "-artifact.raygen.dev",
            "ARTIFACTS_DATA_DIR": "/var/lib/artifacts",
            "ARTIFACTS_OWNER_EMAIL": payload["ownerEmail"],
            "BETTER_AUTH_SECRET": payload["authSecret"],
            "HOST": "0.0.0.0", "PORT": "3000", "NODE_ENV": "production"}


def environment_file(env):
    lines = []
    for key, value in env.items():
        if any(char in value for char in ("\n", "\r", "\0")):
            raise ValueError("Environment values must be single-line")
        # systemd EnvironmentFile is not shell syntax ($ is literal).
        lines.append(key + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"\n')
    return "".join(lines)


def verify_binary(path, digest):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular executable file")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("Executable SHA256 mismatch")


def consul_service(address):
    rule = r"Host(`artifacts.raygen.dev`) || HostRegexp(`^[a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?-artifact\.raygen\.dev$`)"
    return {"service": {"id": "artifacts", "name": "artifacts", "address": address, "port": 3000,
                        "tags": ["traefik.enable=true",
                                 "traefik.http.routers.artifacts.rule=" + rule,
                                 "traefik.http.routers.artifacts.priority=200",
                                 "traefik.http.routers.artifacts.entrypoints=web",
                                 "traefik.http.routers.artifacts.service=artifacts",
                                 "traefik.http.services.artifacts.loadbalancer.server.port=3000",
                                 "traefik.http.services.artifacts.loadbalancer.passhostheader=true"],
                        "check": {"http": "http://127.0.0.1:3000/healthz",
                                  "header": {"Host": ["artifacts.raygen.dev"]},
                                  "interval": "10s", "timeout": "3s", "status": "critical"}}}


def account(name):
    try:
        user = pwd.getpwnam(name)
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", f"/var/lib/{name}",
            "--shell", "/usr/sbin/nologin", name)
        user = pwd.getpwnam(name)
    home = Path(f"/var/lib/{name}")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    os.chown(home, user.pw_uid, user.pw_gid)
    return user


def install_consul():
    target = Path("/usr/local/bin/consul")
    if target.exists():
        version = subprocess.run([str(target), "version"], check=True, capture_output=True, text=True).stdout
        if version.splitlines()[0] == "Consul v1.20.1":
            return
    base = "https://releases.hashicorp.com/consul/1.20.1/"
    filename = "consul_1.20.1_linux_amd64.zip"
    with urllib.request.urlopen(base + "consul_1.20.1_SHA256SUMS", timeout=120) as response:
        sums = response.read().decode()
    digest = next(line.split()[0] for line in sums.splitlines() if line.split()[-1] == filename)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / filename
        with urllib.request.urlopen(base + filename, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        verify_binary(archive, digest)
        with zipfile.ZipFile(archive) as package, package.open("consul") as source, target.with_suffix(".new").open("wb") as output:
            shutil.copyfileobj(source, output)
        target.with_suffix(".new").chmod(0o755)
        os.replace(target.with_suffix(".new"), target)


def install_release(root, binary, digest):
    root.chmod(0o755)
    releases = root / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    releases.chmod(0o755)
    release = releases / digest
    if release.exists():
        verify_binary(release / "artifacts", digest)
    else:
        with tempfile.TemporaryDirectory(dir=releases) as temp:
            candidate = Path(temp) / digest
            candidate.mkdir(mode=0o755)
            candidate.chmod(0o755)
            shutil.copyfile(binary, candidate / "artifacts")
            (candidate / "artifacts").chmod(0o755)
            verify_binary(candidate / "artifacts", digest)
            os.rename(candidate, release)
    return release


def main(payload):
    os.umask(0o077)
    digest = payload["hash"]
    if not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("Invalid release hash")
    env = app_environment(payload)
    rendered = environment_file(env)
    if len(payload["authSecret"]) < 32 or not payload["ownerPassword"] or "@" not in payload["ownerEmail"]:
        raise ValueError("Owner credentials and a >=32-character auth secret are required")
    binary = Path(payload["binary"])
    verify_binary(binary, digest)
    run("apt-get", "update", "-qq")
    run("apt-get", "install", "-y", "--no-install-recommends", "ca-certificates")
    user = account("artifacts")
    consul = account("consul")
    install_consul()
    root = Path("/opt/artifacts")
    release = install_release(root, binary, digest)
    # No in-place release writes and no data-directory replacement. Old releases
    # are intentionally retained for rollback; monitor disk before pruning.
    Path("/etc/artifacts").mkdir(mode=0o700, exist_ok=True)
    Path("/var/log/artifacts-usage").mkdir(mode=0o700, exist_ok=True)
    write("/usr/local/sbin/artifacts-usage", payload["files"]["usage.py"], 0o755)
    for filename in ("artifacts.service", "artifacts-consul.service", "artifacts-usage.service", "artifacts-usage.timer"):
        write("/etc/systemd/system/" + filename, payload["files"][filename], 0o644)
    write("/etc/systemd/journald.conf.d/artifacts.conf",
          "[Journal]\nStorage=persistent\nSystemMaxUse=64M\nRuntimeMaxUse=16M\nMaxRetentionSec=14day\nRateLimitIntervalSec=30s\nRateLimitBurst=1000\n", 0o644)
    run("systemctl", "restart", "systemd-journald")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "artifacts-usage.timer")
    config = Path("/etc/consul.d")
    config.mkdir(mode=0o750, exist_ok=True)
    config.chmod(0o750)
    os.chown(config, 0, consul.pw_gid)
    # Remove route before migrating/restarting an existing installation. A
    # failed bootstrap leaves the route closed; never restore it unhealthily.
    (config / "artifacts-service.json").unlink(missing_ok=True)
    active = subprocess.run(["systemctl", "is-active", "--quiet", "artifacts-consul.service"]).returncode == 0
    if active:
        run("systemctl", "reload", "artifacts-consul.service")
    run("systemctl", "stop", "artifacts.service")
    write("/etc/artifacts/app.env", rendered)
    bootstrap_env = {**env, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/var/lib/artifacts",
                     "ARTIFACTS_OWNER_PASSWORD": payload.pop("ownerPassword")}
    try:
        run(str(release / "artifacts"), "--bootstrap", env=bootstrap_env,
            user=user.pw_uid, group=user.pw_gid, extra_groups=[], cwd="/var/lib/artifacts", timeout=300)
    finally:
        bootstrap_env.pop("ARTIFACTS_OWNER_PASSWORD", None)
    current_new = root / "current.new"
    current_new.unlink(missing_ok=True)
    current_new.symlink_to(release)
    os.replace(current_new, root / "current")
    run("systemctl", "enable", "--now", "artifacts.service")
    healthy = False
    for _ in range(60):
        try:
            request = urllib.request.Request("http://127.0.0.1:3000/healthz", headers={"Host": "artifacts.raygen.dev"})
            with urllib.request.urlopen(request, timeout=3) as response:
                healthy = response.status == 200
            if healthy:
                break
        except OSError:
            pass
        time.sleep(1)
    if not healthy:
        raise RuntimeError("Artifacts health check failed; route remains closed")
    write(config / "agent.json", json.dumps({
        "node_name": "artifacts", "datacenter": "homelab", "server": False,
        "data_dir": "/var/lib/consul", "bind_addr": payload["address"],
        "advertise_addr": payload["address"], "client_addr": "127.0.0.1",
        "retry_join": ["192.168.0.201"], "disable_remote_exec": True,
        "enable_script_checks": False,
    }), owner=consul)
    write(config / "artifacts-service.json", json.dumps(consul_service(payload["address"])), owner=consul)
    run("/usr/local/bin/consul", "validate", str(config))
    run("systemctl", "enable", "artifacts-consul.service")
    run("systemctl", "restart", "artifacts-consul.service")
    run("systemctl", "is-active", "artifacts-consul.service")
