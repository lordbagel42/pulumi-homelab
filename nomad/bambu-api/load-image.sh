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
# Docker's classic and containerd stores expose different kinds of .Id.
# Compare the exact config blob instead; it includes ordered layer DiffIDs.
docker save --output "$staging/image.tar" "$image"
config_path=$(tar -xOf "$staging/image.tar" manifest.json | jq -er --arg image "$image" \
    'if length == 1 and (.[0].RepoTags | index($image) != null) then .[0].Config else error("unexpected image manifest") end')
local_digest=$(tar -xOf "$staging/image.tar" "$config_path" | sha256sum | cut -d ' ' -f 1)
ssh "${ssh_options[@]}" root@192.168.0.230 docker load < "$staging/image.tar"
remote_digest=$(ssh "${ssh_options[@]}" root@192.168.0.230 bash -s -- "$image" <<'REMOTE'
set -euo pipefail
umask 077
archive=$(mktemp /tmp/bambu-image-check.XXXXXX)
trap 'rm -f "$archive"' EXIT
docker save --output "$archive" "$1"
config_path=$(tar -xOf "$archive" manifest.json | jq -er --arg image "$1" \
    'if length == 1 and (.[0].RepoTags | index($image) != null) then .[0].Config else error("unexpected image manifest") end')
tar -xOf "$archive" "$config_path" | sha256sum | cut -d ' ' -f 1
REMOTE
)
printf 'Image config verification: local=%q remote=%q\n' "$local_digest" "$remote_digest"
test "$local_digest" = "$remote_digest"
printf 'Loaded %s with matching config blob and layer DiffIDs\n' "$image"
