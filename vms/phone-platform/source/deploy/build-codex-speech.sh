#!/usr/bin/env bash
# Builds locally; never installs, logs in, restarts a service or changes a PBX.
set -euo pipefail
umask 077
if [[ $# != 1 || -e $1 ]]; then
  echo "Usage: bash build-codex-speech.sh /absolute/unused-build-directory" >&2
  exit 2
fi
if [[ $1 != /* || $(uname -sm) != 'Linux x86_64' ]]; then
  echo "An absolute output path on Linux x86_64 is required." >&2
  exit 2
fi
script_dir=$(realpath "$(dirname "$0")")
patch_file="$script_dir/codex-speech-only.patch"
root=$1
parent=$(dirname "$root")
available=$(df -Pk "$parent" | awk 'NR == 2 {print $4}')
if ((available < 20 * 1024 * 1024)); then
  echo "Build needs at least 20 GiB free for Rust, dependencies and release artifacts." >&2
  exit 2
fi
mkdir -m 700 "$root"
git clone --depth 1 --branch rust-v0.157.1 https://github.com/openai/codex.git "$root/source"
test "$(git -C "$root/source" rev-parse HEAD)" = 36650394c5b38c2990ccf2a3457165ca3e9d9726
git -C "$root/source" apply --check "$patch_file"
git -C "$root/source" apply "$patch_file"

# Preserve the official helper/resource layout; replace only the main binary.
curl --fail --location --proto '=https' --tlsv1.2 \
  https://github.com/openai/codex/releases/download/rust-v0.157.1/codex-package-x86_64-unknown-linux-musl.tar.gz \
  --output "$root/bundle.tar.gz"
printf '%s  %s\n' 0e211868c9fd73cb49ad35ac675b5eafdf6b9f453df8a493df980c59a590fe5f "$root/bundle.tar.gz" | sha256sum --check
mkdir "$root/bundle"
tar -xzf "$root/bundle.tar.gz" -C "$root/bundle"

# Preserve the embedded integrity check for the official sandbox helper.
CODEX_BWRAP_SHA256=$(sha256sum "$root/bundle/codex-resources/bwrap" | cut -d ' ' -f 1)
export CODEX_BWRAP_SHA256

# Release tags bump workspace versions without updating Cargo.lock. Reconcile
# those versions, but refuse any change to registry or Git dependency pins.
cp "$root/source/codex-rs/Cargo.lock" "$root/Cargo.lock.upstream"
(
  cd "$root/source/codex-rs"
  cargo update --workspace
)
python3 "$script_dir/verify-codex-lock.py" \
  "$root/Cargo.lock.upstream" "$root/source/codex-rs/Cargo.lock"

# Use upstream's pinned Rust toolchain; CI can reduce concurrency for its RAM.
(
  cd "$root/source/codex-rs"
  CARGO_TARGET_DIR="$root/target" cargo build --locked --release --jobs "${CARGO_BUILD_JOBS:-2}" --bin codex
)
install -m 700 "$root/target/release/codex" "$root/bundle/bin/codex"
test "$("$root/bundle/bin/codex" --version)" = 'codex-cli 0.157.1'
cp "$root/source/codex-rs/Cargo.lock" "$root/bundle/phone-speech-Cargo.lock"
cp "$patch_file" "$root/bundle/phone-speech.patch"
cp "$root/source/LICENSE" "$root/bundle/LICENSE"
if [[ -f "$root/source/NOTICE" ]]; then
  cp "$root/source/NOTICE" "$root/bundle/NOTICE"
fi

# The service verifies this digest before starting a process. This is a local
# build receipt, not a signature: protect the bundle and receipt from writes.
python3 - "$root/bundle" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

bundle = Path(sys.argv[1])

def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

(bundle / "phone-speech-build.json").write_text(json.dumps({
    "policy": "realtime-no-delegation-v1", "version": "0.157.1",
    "sha256": digest(bundle / "bin/codex"),
    "source": "36650394c5b38c2990ccf2a3457165ca3e9d9726",
    "lock_sha256": digest(bundle / "phone-speech-Cargo.lock"),
    "patch_sha256": digest(bundle / "phone-speech.patch"),
    "bwrap_sha256": os.environ["CODEX_BWRAP_SHA256"],
    "release_profile_overrides": {
        name: value for name, value in os.environ.items()
        if name.startswith("CARGO_PROFILE_RELEASE_")
    },
}) + "\n")
PY
printf 'Built speech-only bundle: %s/bundle\nNo account login or deployment performed.\n' "$root"
