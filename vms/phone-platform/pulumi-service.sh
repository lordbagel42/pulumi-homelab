#!/bin/bash
# Target only reviewed application resources on the existing phone VM.
set -euo pipefail
phase=${1:-platform}
action=${2:-preview}
if [ "$#" -ge 2 ]; then shift 2; else set --; fi
case "$action" in preview|up) ;; *) echo 'Action must be preview or up' >&2; exit 2;; esac
prefix='urn:pulumi:homelab::pulumi-homelab'
targets=()
case "$phase" in
  platform) module=phone-platform; resources=(stage setup);;
  huddle) module=huddle-phone; resources=(stage environment deploy);;
  voice) module=phone-voice; resources=(stage setup);;
  *) echo 'Usage: pulumi-service.sh [platform|huddle|voice] [preview|up] [Pulumi flags]' >&2; exit 2;;
esac
targets+=(--target "$prefix::command:remote:CopyToRemote::$module-source")
for resource in "${resources[@]}"; do
  targets+=(--target "$prefix::command:remote:Command::$module-$resource")
done
exec env -u SSH_AUTH_SOCK pulumi "$action" --stack homelab "${targets[@]}" "$@"
