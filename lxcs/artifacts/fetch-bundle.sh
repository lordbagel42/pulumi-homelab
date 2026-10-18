#!/usr/bin/env bash
set -euo pipefail

# Recover the exact deployed, immutable binary, not whichever release is current.
# CI has the same ESC-provided SSH identity as Pulumi's remote commands.
digest=a3894655a7185624c08af820843b6d8bb6763c9f3a2fe7ca1796d33a52bfa0a6
umask 077
credentials=$(mktemp -d)
trap 'rm -rf "$credentials"' EXIT
pulumi config get SSH_PRIVATE_KEY --stack homelab > "$credentials/key"
host_key=$(pulumi config get artifacts:sshHostKey --stack homelab)
printf '192.168.0.216 %s\n' "$host_key" > "$credentials/known_hosts"
mkdir -p .amp/in/artifacts-bundle
chmod 0700 .amp/in/artifacts-bundle
scp -q -i "$credentials/key" -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$credentials/known_hosts" \
    -o ConnectTimeout=15 \
    "root@192.168.0.216:/opt/artifacts/releases/$digest/artifacts" \
    .amp/in/artifacts-bundle/artifacts
printf '%s  %s\n' "$digest" .amp/in/artifacts-bundle/artifacts | sha256sum --check
