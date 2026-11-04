#!/usr/bin/env bash
set -euo pipefail

# Use the same pinned image and SSH identity as the Pulumi-owned job.
image=$(pulumi config get bambu-api:image --stack homelab)
if [[ ! "$image" =~ ^bambu-api:([a-f0-9]{40})$ ]]; then
    echo "Bambu image loading requires a full Git commit tag" >&2
    exit 1
fi
revision=${BASH_REMATCH[1]}
umask 077
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
git clone --quiet https://github.com/lordbagel42/bambu-rea.git "$staging/source"
git -C "$staging/source" checkout --quiet --detach "$revision"
test "$(git -C "$staging/source" rev-parse HEAD)" = "$revision"
docker build --build-arg VERSION="$revision" -t "$image" "$staging/source"
pulumi config get SSH_PRIVATE_KEY --stack homelab > "$staging/key"
host_key=$(pulumi config get bambu-api:sshHostKey --stack homelab)
printf '192.168.0.230 %s\n' "$host_key" > "$staging/known_hosts"
ssh_options=(-i "$staging/key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$staging/known_hosts" -o ConnectTimeout=15)
docker save "$image" | ssh "${ssh_options[@]}" root@192.168.0.230 docker load
local_id=$(docker image inspect --format '{{.Id}}' "$image")
remote_id=$(ssh "${ssh_options[@]}" root@192.168.0.230 \
    docker image inspect --format '{{.Id}}' "$image")
test "$local_id" = "$remote_id"
printf 'Loaded %s with matching image ID %s\n' "$image" "$local_id"
