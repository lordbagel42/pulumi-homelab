#!/usr/bin/env bash
set -euo pipefail

# Use the same pinned image and SSH identity as the Pulumi-owned job.
umask 077
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
# Each config invocation opens ESC/Infisical. Resolve once, privately, and retry
# only an explicit read-side rate limit rather than repeating any deployment.
for attempt in 1 2 3; do
    if pulumi config --stack homelab --show-secrets --json \
        > "$staging/config.json" 2> "$staging/config-error"; then
        break
    fi
    if ! grep -q 'status-code=429' "$staging/config-error" || [ "$attempt" = 3 ]; then
        echo "Cannot resolve Pulumi deployment configuration; no image loaded" >&2
        exit 1
    fi
    echo "Infisical rate limited configuration read; waiting 30 seconds" >&2
    sleep 30
done
image=$(jq -er '."bambu-api:image".value' "$staging/config.json")
if [[ ! "$image" =~ ^bambu-api:([a-f0-9]{40})$ ]]; then
    echo "Bambu image loading requires a full Git commit tag" >&2
    exit 1
fi
revision=${BASH_REMATCH[1]}
git clone --quiet https://github.com/lordbagel42/bambu-rea.git "$staging/source"
git -C "$staging/source" checkout --quiet --detach "$revision"
test "$(git -C "$staging/source" rev-parse HEAD)" = "$revision"
docker build --build-arg VERSION="$revision" -t "$image" "$staging/source"
jq -er '."pulumi-homelab:SSH_PRIVATE_KEY".value' "$staging/config.json" > "$staging/key"
host_key=$(jq -er '."bambu-api:sshHostKey".value' "$staging/config.json")
printf '192.168.0.230 %s\n' "$host_key" > "$staging/known_hosts"
ssh_options=(-i "$staging/key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$staging/known_hosts" -o ConnectTimeout=15)
docker save "$image" | ssh "${ssh_options[@]}" root@192.168.0.230 docker load
local_id=$(docker image inspect --format '{{.Id}}' "$image")
remote_id=$(ssh "${ssh_options[@]}" root@192.168.0.230 \
    docker image inspect --format '{{.Id}}' "$image")
test "$local_id" = "$remote_id"
printf 'Loaded %s with matching image ID %s\n' "$image" "$local_id"
