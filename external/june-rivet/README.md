# June Rivet dashboard

Deployed on 2026-09-27 using only the five resources in this module. The existing
Cloudflare API token needed account-level Cloudflare Tunnel Write; deployment
succeeded after that permission became effective.

`https://june-rivet.raygen.dev` redirects to Rivet's `/ui/`. The complete hostname,
including API, health, assets, OPTIONS, and WebSocket requests, requires
Cloudflare Access. Only `raygenrrupe@gmail.com` is allowed, matching the existing
June console restriction. There are no bypass or service-token policies.
Rivet's own admin-token authentication remains required after Access login.
Never put that token in a URL, repository, screenshot, or general log.

A dedicated remotely managed tunnel runs on June (LXC 215) and connects directly
to `127.0.0.1:6420`. Rivet stays loopback-only. The connector checks the Access
JWT's signature and this application's audience. Unknown hostnames return 404;
there is no shared Traefik route, LAN proxy, or alternate origin listener.

This module does not modify June's application, config, deployment controller,
existing console, Slack ingress, or shared Cloudflare tunnel. It installs only
`june-rivet-tunnel.service`, a checksum-pinned cloudflared binary, and a private
connector token loaded via systemd credentials. Metrics/readiness listen only
on `127.0.0.1:20242`. The connector runs as a separate dynamic user without
access to June's configuration, credentials, releases, or state.

## Scoped deployment

The Cloudflare API token needs DNS edit and zone read for `raygen.dev`, Access
Apps and Policies edit, and Cloudflare Tunnel write for the zone's account.
The SSH host key is the existing console-verified June Ed25519 key.

Run from the repository root:

```sh
export PATH=/home/amp/.pulumi/bin:$PATH
pnpm test
bash external/june-rivet/deploy.sh preview --diff
# After inspecting the five-resource preview and deployment authorization:
bash external/june-rivet/deploy.sh up --diff
```

**Do not run an untargeted update.** Other worktrees contain locally maintained
resources absent from this branch. Do not select providers, target-dependents,
the June LXC, or its old provisioning resources. DNS waits for Access and a
ready connector; connector startup waits for its Access-validating tunnel config.

## Validation and rollback

- Unauthenticated GET/POST to `/`, `/ui/`, `/namespaces`, and arbitrary paths
  must redirect to Access, including requests with forged JWT/forwarded headers.
  Valid CORS preflight OPTIONS requests must be denied (403), not bypass Access.
- A direct LAN request to June port 6420 must fail to connect.
- After authorized Access login, use the existing Rivet admin token in the UI.
  Read-only `GET /namespaces` on the origin must return 401 without a valid token
  and 200 with it. Suppress bodies and never print the token while checking.
- Check June `/health`, `june.service`, and `june-deploy.service`; this rollout
  must not restart or replace them. Check the connector's `/ready` locally.

For authorized rollback, stop and disable only `june-rivet-tunnel.service`
**before** removing the tunnel route, DNS, or Access application. Keep Access
until no origin can serve the hostname; wildcard DNS may still resolve it.
The Pulumi remote command intentionally has no destructive delete action.
