#!/usr/bin/env bash
# Never deploy unrelated or stale June application resources from this checkout.
set -euo pipefail
action=${1:-preview}
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in preview|up) ;; *) echo 'Action must be preview or up' >&2; exit 2;; esac
prefix='urn:pulumi:homelab::pulumi-homelab'
exec env -u SSH_AUTH_SOCK pulumi "$action" --stack homelab \
    --target "$prefix::cloudflare:index/zeroTrustAccessApplication:ZeroTrustAccessApplication::june-rivet-access" \
    --target "$prefix::cloudflare:index/zeroTrustTunnelCloudflared:ZeroTrustTunnelCloudflared::june-rivet-tunnel" \
    --target "$prefix::cloudflare:index/zeroTrustTunnelCloudflaredConfig:ZeroTrustTunnelCloudflaredConfig::june-rivet-tunnel-config" \
    --target "$prefix::command:remote:Command::june-rivet-connector" \
    --target "$prefix::cloudflare:index/dnsRecord:DnsRecord::june-rivet-dns" \
    "$@"
