#!/bin/bash
# Select only June's machine, application, and/or webhook ingress resources.
set -euo pipefail

phase=${1:-all}
if [ "$#" -gt 0 ]; then shift; fi
action=${1:-preview}
if [ "$#" -gt 0 ]; then shift; fi
case "$phase" in machine|service|ingress|all) ;; *) echo 'Phase must be machine, service, ingress, or all' >&2; exit 2;; esac
case "$action" in preview|up) ;; *) echo 'Action must be preview or up' >&2; exit 2;; esac

prefix='urn:pulumi:homelab::pulumi-homelab'
machine_targets=(
    --target "$prefix::homelab:framework:ProxmoxMachine::june"
    --target "$prefix::homelab:framework:ProxmoxMachine\$proxmoxve:index/containerLegacy:ContainerLegacy::june"
)
service_targets=(
    --target "$prefix::homelab:framework:ProxmoxMachine\$command:remote:Command::june-stage"
    --target "$prefix::homelab:framework:ProxmoxMachine\$command:remote:CopyToRemote::june-source"
    --target "$prefix::homelab:framework:ProxmoxMachine\$command:remote:Command::june-provision"
)
ingress_targets=(
    --target "$prefix::consul:index/node:Node::june-slack-node"
    --target "$prefix::consul:index/service:Service::june-slack-service"
    --target "$prefix::cloudflare:index/dnsRecord:DnsRecord::june-slack-dns"
)
targets=()
case "$phase" in
    machine) targets+=("${machine_targets[@]}");;
    service) targets+=("${service_targets[@]}");;
    ingress) targets+=("${ingress_targets[@]}");;
    all) targets+=("${machine_targets[@]}" "${service_targets[@]}" "${ingress_targets[@]}");;
esac

exec env -u SSH_AUTH_SOCK pulumi "$action" --stack homelab "${targets[@]}" "$@"
