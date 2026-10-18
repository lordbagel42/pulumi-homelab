# Private June console

Routing only: June at `192.168.0.215:3080`, with its existing owner-session
authentication. This does not provision the LXC, change runtime config, or touch
the deployment controller or Slack webhook route. Console origin must be
`https://june.raygen.dev`.

Cloudflare Access allows only `raygenrrupe@gmail.com`. All three Traefik routers
require cloudflared's socket peer `192.168.0.204/32`, never forwarded headers:

- Priority 300: `/console` and `/console/…` reach June.
- The same router also forwards GET/HEAD for exactly one root segment of 24
  characters from `[A-Za-z0-9_-]`. June redirects these one-time login links to
  `/console/session/link/…`; only a confirmed POST on that console path consumes
  the credential. Other root methods, lengths, and nested paths remain denied.
- Priority 300: `/` redirects to the fixed `/console/session/login` URL.
- Priority 200: all other paths hit `noop@internal` (418), above Authentik at 100.
- All three disable access logs to keep OAuth callbacks and login links out of logs.

## Scoped rollout

**Never run an untargeted update from this checkout.** It does not contain all
locally maintained June resources in stack state. Do not use target-dependents,
machine/provisioning/provider targets, or the old June provisioning archive.

The old unproxied CNAME was removed externally during preparation. Authoritative
Cloudflare inspection confirmed only the original TXT record remained. This
module creates the missing CNAME without touching TXT or wildcard records.

```bash
export PATH=/home/amp/.pulumi/bin:$PATH
targets=(
  --target 'urn:pulumi:homelab::pulumi-homelab::cloudflare:index/zeroTrustAccessApplication:ZeroTrustAccessApplication::june-console-access'
  --target 'urn:pulumi:homelab::pulumi-homelab::cloudflare:index/dnsRecord:DnsRecord::june-console-dns'
  --target 'urn:pulumi:homelab::pulumi-homelab::consul:index/node:Node::june-console-node'
  --target 'urn:pulumi:homelab::pulumi-homelab::consul:index/service:Service::june-console-service'
)
pulumi preview --stack homelab --diff "${targets[@]}"
# Only after authorization and inspection:
pulumi up --stack homelab --diff "${targets[@]}"
```

Initial scoped rollout: four resources created, 144 unchanged, no replacements
or deletes. DNS waits for Access; the catalog service waits for DNS.

For routing-only updates, target just the existing catalog service:

```bash
target='urn:pulumi:homelab::pulumi-homelab::consul:index/service:Service::june-console-service'
pulumi preview --stack homelab --diff --target "$target"
# Only after authorization and inspection:
pulumi up --stack homelab --diff --target "$target"
```

## Verification

- Unauthenticated HTTPS prompts for Cloudflare login; allowed identity still
  requires June's owner session.
- From the tunnel peer, root redirects, console reaches June, and `/api`,
  `/health`, `/consolex`, `/webhooks/slack` and Authentik paths return 418.
- Direct requests from other peers return 403, including forged forwarded IPs.
- Existing signed Slack webhook ingress and DNS TXT/wildcard records remain intact.

Disable this catalog service before removing Access during authorized rollback;
wildcard DNS can still resolve the hostname. Do not disturb the app, controller,
other routes or unrelated records.
