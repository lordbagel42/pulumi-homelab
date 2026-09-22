#!/bin/bash
# Run from the repository root. Explicit targets exclude other phone services
# that may depend on this VM or on its deployment command.
set -euo pipefail
action=${1:-preview}
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in
    preview|up) ;;
    *) echo 'Usage: bash vms/huddle-phone/pulumi-service.sh [preview|up] [Pulumi flags]' >&2; exit 2 ;;
esac

prefix='urn:pulumi:homelab::pulumi-homelab'
targets=(
    --target "$prefix::homelab:framework:ProxmoxMachine::huddle-phone"
    --target "$prefix::homelab:framework:ProxmoxMachine\$proxmoxve:index/vmLegacy:VmLegacy::huddle-phone"
)
# Read presence locally; a config/network error must fail rather than silently
# turn an application deployment into a VM-only update.
host_key_present=$(node -e '
const fs = require("fs"), yaml = require("js-yaml");
const config = yaml.load(fs.readFileSync("Pulumi.homelab.yaml", "utf8")).config;
console.log(Boolean(config["huddle-phone:sshHostKey"]));
')
if [ "$host_key_present" = true ]; then
    targets+=(--target "$prefix::command:remote:CopyToRemote::huddle-phone-source")
    for resource in host stage environment deploy; do
        targets+=(--target "$prefix::command:remote:Command::huddle-phone-$resource")
    done
fi
exec env -u SSH_AUTH_SOCK pulumi "$action" --stack homelab "${targets[@]}" "$@"
