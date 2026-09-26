"""Install or update June without exposing credentials in command arguments."""

import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


NODE_VERSION = "24.21.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
NODE_ROOT = Path(f"/opt/node-v{NODE_VERSION}")
PNPM_VERSION = "10.33.0"
JUNE_ROOT = Path("/opt/june")
RELEASES = JUNE_ROOT / "releases"
CURRENT = JUNE_ROOT / "current"
STATE = Path("/var/lib/june")
CONFIG = Path("/etc/june/config.json")
CREDENTIALS = Path("/etc/june/credentials")
SERVICE = Path("/etc/systemd/system/june.service")
LOGIN_HELPER = Path("/usr/local/sbin/june-start-login")
HEALTH_URL = "http://192.168.0.215:3080/health"

PUBLIC_TOP_LEVEL_FILES = {
    ".node-version",
    ".npmrc",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
}
PUBLIC_TOP_LEVEL_DIRECTORIES = {"src"}
REQUIRED_SOURCE_FILES = {
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "src/main.ts",
}
PRIVATE_OR_GENERATED_NAMES = {
    ".codex",
    ".data",
    ".env",
    ".git",
    ".pnpm-store",
    "config.local.json",
    "node_modules",
}


def run(*arguments, cwd=None, env=None, check=True):
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=check,
        stdin=subprocess.DEVNULL,
    )


def output(*arguments, cwd=None, env=None):
    return subprocess.check_output(
        arguments,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
    ).strip()


def write(path, content, mode=0o644, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        staged.write(data)
        staged.flush()
        os.fchmod(staged.fileno(), mode)
        if owner:
            os.fchown(staged.fileno(), owner.pw_uid, owner.pw_gid)
    os.replace(staged.name, path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_name(name):
    if not name or "\\" in name or name.startswith("/") or "\0" in name:
        raise ValueError("Source archive path is unsafe")
    parts = [part for part in PurePosixPath(name).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("Source archive path escapes its root")
    return "/".join(parts)


def validate_source_archive(path):
    """Validate the intentionally small, public deployment snapshot."""
    seen = {}
    total_size = 0
    with tarfile.open(path, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 10_000:
            raise ValueError("Source archive has too many entries")
        for member in members:
            name = archive_name(member.name)
            if not name:
                if not member.isdir():
                    raise ValueError("Source archive root must be a directory")
                continue
            if name in seen:
                raise ValueError("Source archive contains duplicate paths")
            seen[name] = member
            parts = name.split("/")
            if any(part in PRIVATE_OR_GENERATED_NAMES for part in parts):
                raise ValueError(f"{name} is not in the allowlisted public source layout")
            top = parts[0]
            if len(parts) == 1:
                allowed = (
                    member.isfile() and top in PUBLIC_TOP_LEVEL_FILES
                ) or (
                    member.isdir() and top in PUBLIC_TOP_LEVEL_DIRECTORIES
                )
            else:
                allowed = top in PUBLIC_TOP_LEVEL_DIRECTORIES
            if not allowed:
                raise ValueError(f"{name} is not in the allowlisted public source layout")
            if not (member.isfile() or member.isdir()):
                raise ValueError("Source archive links and special files are forbidden")
            if member.mode & (stat.S_ISUID | stat.S_ISGID):
                raise ValueError("Source archive contains privileged mode bits")
            if member.isfile():
                total_size += member.size
                if member.size > 64 * 1024 * 1024 or total_size > 512 * 1024 * 1024:
                    raise ValueError("Source archive exceeds the deployment size limit")
        missing = REQUIRED_SOURCE_FILES.difference(seen)
        if missing or any(not seen[name].isfile() for name in REQUIRED_SOURCE_FILES if name in seen):
            raise ValueError("Source archive is missing required runtime files")
        package_file = source.extractfile(seen["package.json"])
        if package_file is None:
            raise ValueError("Source archive package.json is unreadable")
        package = json.load(package_file)
        if package.get("packageManager") != f"pnpm@{PNPM_VERSION}":
            raise ValueError(f"Source must pin packageManager to pnpm@{PNPM_VERSION}")
    return seen


def normalize_tree(root):
    """Make a public release root-owned, readable, and immutable to June."""
    for current, directories, files in os.walk(root, followlinks=False):
        paths = [Path(current), *(Path(current) / name for name in directories + files)]
        for path in paths:
            metadata = path.lstat()
            os.chown(path, 0, 0, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                continue
            mode = 0o755 if stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o111 else 0o644
            os.chmod(path, mode, follow_symlinks=False)


def is_immutable_entry(metadata, owner_uid=0, owner_gid=0):
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        return False
    # Symlink permissions are not used for access control and conventionally
    # appear as 0777. Their root ownership and root-owned target are the guard.
    return stat.S_ISLNK(metadata.st_mode) or not metadata.st_mode & 0o022


def validate_existing_release(release, revision):
    marker = release / ".june-release"
    if not release.is_dir() or release.is_symlink() or marker.read_text().strip() != revision:
        raise ValueError("Existing June release is incomplete or does not match its revision")
    for path in (release / "src/main.ts", release / "node_modules/.bin/tsx", release / "node_modules/.bin/codex"):
        if not path.is_file():
            raise ValueError(f"Existing June release is missing {path.relative_to(release)}")
    for current, directories, files in os.walk(release, followlinks=False):
        for path in [Path(current), *(Path(current) / name for name in directories + files)]:
            metadata = path.lstat()
            if not is_immutable_entry(metadata):
                raise ValueError("Existing June release is not immutable and root-owned")


def validate_node_archive(source, destination):
    expected_root = f"node-v{NODE_VERSION}-linux-x64"
    for member in source.getmembers():
        name = archive_name(member.name)
        if not name or name.split("/", 1)[0] != expected_root:
            raise ValueError("Unexpected Node.js archive layout")
        if member.isdev() or member.isfifo():
            raise ValueError("Unexpected special file in Node.js archive")
        # data_filter rejects absolute/escaping link targets and strips unsafe
        # ownership and mode metadata while preserving Node's internal links.
        tarfile.data_filter(member, destination)


def install_node():
    marker = NODE_ROOT / ".june-node-sha256"
    if NODE_ROOT.exists():
        if marker.read_text().strip() != NODE_SHA256:
            raise ValueError("Existing pinned Node.js installation is unmanaged or incomplete")
        if output(str(NODE_ROOT / "bin/node"), "--version") != f"v{NODE_VERSION}":
            raise ValueError("Existing pinned Node.js installation has the wrong version")
        return

    with tempfile.TemporaryDirectory(prefix="june-node-download-") as folder:
        archive = Path(folder) / NODE_ARCHIVE
        digest = hashlib.sha256()
        request = urllib.request.Request(NODE_URL, headers={"User-Agent": "june-provisioner/1"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != NODE_SHA256:
            raise ValueError("Node.js release checksum did not match")

        Path("/opt").mkdir(mode=0o755, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".june-node-stage-", dir="/opt") as stage_name:
            stage = Path(stage_name)
            with tarfile.open(archive, "r:xz") as source:
                validate_node_archive(source, stage)
                source.extractall(stage, filter="data")
            extracted = stage / f"node-v{NODE_VERSION}-linux-x64"
            if output(str(extracted / "bin/node"), "--version") != f"v{NODE_VERSION}":
                raise ValueError("Downloaded Node.js executable has the wrong version")
            write(extracted / ".june-node-sha256", NODE_SHA256 + "\n", 0o644)
            normalize_tree(extracted)
            os.replace(extracted, NODE_ROOT)


def install_dependencies(release):
    corepack = NODE_ROOT / "bin/corepack"
    environment = os.environ.copy()
    environment.update({
        "CI": "true",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "COREPACK_HOME": str(JUNE_ROOT / "corepack"),
        "HOME": "/root",
        "PATH": f"{NODE_ROOT / 'bin'}:/usr/local/bin:/usr/bin:/bin",
    })
    (JUNE_ROOT / "corepack").mkdir(mode=0o755, parents=True, exist_ok=True)
    run(str(corepack), "install", "--global", f"pnpm@{PNPM_VERSION}", env=environment)
    if output(str(corepack), "pnpm", "--version", cwd=release, env=environment) != PNPM_VERSION:
        raise ValueError("Corepack did not select the pinned pnpm release")
    run(
        str(corepack), "pnpm", "install", "--frozen-lockfile", "--prod=false",
        cwd=release,
        env=environment,
    )
    tsx = release / "node_modules/.bin/tsx"
    codex = release / "node_modules/.bin/codex"
    if not tsx.is_file() or not codex.is_file():
        raise ValueError("Frozen install did not provide the required tsx and codex executables")
    run(str(tsx), "--version", cwd=release, env=environment)
    run(str(codex), "--version", cwd=release, env=environment)


def prepare_release(archive, revision):
    release = RELEASES / revision
    if release.exists() or release.is_symlink():
        validate_existing_release(release, revision)
        return release

    ensure_public_directory(RELEASES)
    stage = Path(tempfile.mkdtemp(prefix=f".{revision}.stage-", dir=RELEASES))
    try:
        with tarfile.open(archive, "r:gz") as source:
            validate_source_archive(archive)
            source.extractall(stage, filter="data")
        install_dependencies(stage)
        write(stage / ".june-release", revision + "\n", 0o644)
        normalize_tree(stage)
        os.replace(stage, release)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    validate_existing_release(release, revision)
    return release


def ensure_public_directory(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"Public installation path is not a real directory: {path}") from error
    try:
        os.fchown(descriptor, os.geteuid(), os.getegid())
        # mkdir honors the process umask; force traversal for the service user.
        os.fchmod(descriptor, 0o755)
    finally:
        os.close(descriptor)


def ensure_private_directory(path, account):
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"Private state path is not a real directory: {path}") from error
    try:
        os.fchown(descriptor, account.pw_uid, account.pw_gid)
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def ensure_account():
    try:
        account = pwd.getpwnam("june")
    except KeyError:
        run(
            "useradd", "--system", "--user-group", "--create-home", "--home-dir", str(STATE),
            "--shell", "/usr/sbin/nologin", "june",
        )
        account = pwd.getpwnam("june")
    try:
        group = grp.getgrnam("june")
    except KeyError as error:
        raise ValueError("Existing june account has no dedicated group") from error
    if account.pw_uid == 0 or account.pw_gid != group.gr_gid or account.pw_dir != str(STATE):
        raise ValueError("Existing june account has an unsafe identity or home")
    for directory in (STATE, STATE / ".codex", STATE / "rivet"):
        ensure_private_directory(directory, account)
    return account


def snapshot(path):
    path = Path(path)
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing to replace non-regular configuration path {path}")
    metadata = path.stat()
    return (path.read_bytes(), stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)


def restore(path, saved):
    path = Path(path)
    if saved is None:
        path.unlink(missing_ok=True)
        return
    content, mode, uid, gid = saved
    write(path, content, mode)
    os.chown(path, uid, gid)


def current_target():
    if not os.path.lexists(CURRENT):
        return None
    if not CURRENT.is_symlink():
        raise ValueError("/opt/june/current must be a managed symbolic link")
    target = (CURRENT.parent / os.readlink(CURRENT)).resolve()
    if not re.fullmatch(r"/opt/june/releases/[0-9a-f]{64}", str(target)) or not target.is_dir():
        raise ValueError("/opt/june/current points outside the managed releases")
    return target


def switch_current(target):
    staged = CURRENT.with_name(f".current.{os.getpid()}")
    staged.unlink(missing_ok=True)
    staged.symlink_to(target, target_is_directory=True)
    os.replace(staged, CURRENT)


def service_state(action):
    return run("systemctl", action, "--quiet", "june.service", check=False).returncode == 0


def wait_for_health(timeout=120):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(HEALTH_URL, timeout=3) as response:
                body = json.loads(response.read(4096))
                if response.status == 200 and body.get("name") == "June" and body.get("ready") is True:
                    return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    return False


def activate(release, payload):
    previous = current_target()
    saved = {
        CONFIG: snapshot(CONFIG),
        CREDENTIALS: snapshot(CREDENTIALS),
        SERVICE: snapshot(SERVICE),
        LOGIN_HELPER: snapshot(LOGIN_HELPER),
    }
    was_enabled = service_state("is-enabled")
    was_active = service_state("is-active")

    try:
        ensure_public_directory(CONFIG.parent)
        write(CONFIG, payload["config"], 0o644)
        credentials = f"JUNE_OPERATOR_TOKEN={payload['operatorToken']}\n"
        if payload.get("slack") is not None:
            credentials += f"SLACK_BOT_TOKEN={payload['slack']['botToken']}\n"
            credentials += f"SLACK_SIGNING_SECRET={payload['slack']['signingSecret']}\n"
        write(CREDENTIALS, credentials, 0o600)
        write(SERVICE, payload["service"], 0o644)
        write(LOGIN_HELPER, payload["startLogin"], 0o755)
        run("systemctl", "daemon-reload")
        # Stop before moving the stable link so the old process cannot resolve
        # an executable through the new release. The cgroup stop also reaps the
        # detached Rivet engine before a new listener can start.
        run("systemctl", "stop", "june.service")
        switch_current(release)
        run("systemctl", "enable", "june.service")
        run("systemctl", "start", "june.service")
        if not wait_for_health():
            raise RuntimeError("June did not become ready")
    except BaseException as failure:
        # Stop the whole cgroup first. KillMode=control-group includes Rivet's
        # detached engine, so a restart cannot leave a duplicate listener.
        run("systemctl", "stop", "june.service", check=False)
        if previous is None:
            CURRENT.unlink(missing_ok=True)
        else:
            switch_current(previous)
        for path, contents in saved.items():
            restore(path, contents)
        run("systemctl", "daemon-reload")
        if was_enabled:
            run("systemctl", "enable", "june.service")
        else:
            run("systemctl", "disable", "june.service", check=False)
        if was_active and previous is not None and saved[SERVICE] is not None:
            run("systemctl", "restart", "june.service")
            if not wait_for_health(60):
                raise RuntimeError("June update failed and the previous release was restored but is not ready") from failure
        raise RuntimeError("June update failed; the previous service and current link were restored") from failure


def validate_payload(payload):
    revision = payload.get("revision", "")
    archive = Path(payload.get("archive", ""))
    expected_archive = JUNE_ROOT / "incoming" / f"{revision}.tar.gz"
    if not re.fullmatch(r"[0-9a-f]{64}", revision) or archive != expected_archive:
        raise ValueError("Unexpected June archive or release revision")
    if not archive.is_file() or archive.is_symlink() or sha256(archive) != revision:
        raise ValueError("June source archive checksum did not match")
    token = payload.get("operatorToken", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("June operator token must be a URL-safe value of 32-256 characters")
    expected_config = {
        "host": "192.168.0.215",
        "port": 3080,
        "setupMode": True,
        "owner": {"id": "raygen", "identities": []},
        "model": {
            "protocol": "codex",
            "model": "gpt-6-astra",
            "home": "/var/lib/june/.codex",
            "executable": "/opt/june/current/node_modules/.bin/codex",
        },
        "coding": {"enabled": False},
    }
    slack = payload.get("slack")
    if slack is not None:
        if not isinstance(slack, dict) or set(slack) != {
            "teamId", "botUserId", "ownerUserId", "botToken", "signingSecret",
        }:
            raise ValueError("Unexpected June Slack configuration")
        patterns = {
            "teamId": r"T[A-Z0-9]+",
            "botUserId": r"[UW][A-Z0-9]+",
            "ownerUserId": r"[UW][A-Z0-9]+",
            "botToken": r"[A-Za-z0-9._-]{16,512}",
            "signingSecret": r"[a-fA-F0-9]{32}",
        }
        for key, pattern in patterns.items():
            if not isinstance(slack[key], str) or not re.fullmatch(pattern, slack[key]):
                raise ValueError(f"Invalid June Slack {key}")
        if slack["botUserId"] == slack["ownerUserId"]:
            raise ValueError("June's Slack owner must be distinct from the bot")
        expected_config.update({
            "setupMode": False,
            "owner": {"id": "raygen", "identities": [{
                "channel": "slack", "accountId": slack["teamId"], "senderId": slack["ownerUserId"],
            }]},
            "slack": {
                "teamId": slack["teamId"], "botUserId": slack["botUserId"],
                "signingSecretEnv": "SLACK_SIGNING_SECRET", "botTokenEnv": "SLACK_BOT_TOKEN",
            },
        })
    if json.loads(payload.get("config", "null")) != expected_config:
        raise ValueError("Unexpected June runtime configuration")
    for key in ("service", "startLogin"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"Missing June {key} asset")
    validate_source_archive(archive)
    return archive, revision


def main(payload):
    os.umask(0o077)
    if os.geteuid() != 0:
        raise PermissionError("June provisioning must run as root")
    archive, revision = validate_payload(payload)
    run("apt-get", "update", "-qq")
    run(
        "apt-get", "install", "-y", "--no-install-recommends",
        "ca-certificates", "xz-utils",
    )
    ensure_account()
    ensure_public_directory(JUNE_ROOT)
    install_node()
    release = prepare_release(archive, revision)
    activate(release, payload)
    print(f"June release {revision} is ready on {HEALTH_URL}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--validate-source-archive":
        try:
            validate_source_archive(Path(sys.argv[2]))
        except Exception as error:
            print(f"Source archive validation failed: {error}", file=sys.stderr)
            raise SystemExit(1)
        print(sha256(Path(sys.argv[2])))
    else:
        main(json.load(sys.stdin))
