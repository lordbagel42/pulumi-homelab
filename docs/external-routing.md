# External Routing

How a request from the internet reaches a service. There are two independent
edges — the homelab and the Oracle VM — each with its own Cloudflare tunnel and
its own Traefik, both backed by the **same** Consul datacenter (`homelab`).

```
                    ┌─ homelab ────────────────────────────────────────────┐
internet ─▶ CF tunnel ─▶ cloudflared (204) ─▶ traefik (203) ─▶ nomad-client (230)
                    │                              ▲                 authentik (220)
                    │                              └── routes from Consul catalog
                    └──────────────────────────────────────────────────────┘

                    ┌─ oracle ─────────────────────────────────────────────┐
internet ─▶ CF tunnel ─▶ cloudflared (job) ─▶ traefik (job) ─▶ pelican (job)
                    └──────────────────────────────────────────────────────┘
```

Neither edge exposes a port to the LAN's WAN address — the tunnels dial out, so
there is no inbound port forward anywhere.

## How a service becomes externally reachable

1. **Register in Consul with Traefik tags.** Either declare `reverseProxy` on a
   `ProxmoxMachine` (see `docs/adding-a-service.md`) or put the tags on a Nomad
   `service` block directly, as `nomad/lookout/lookout.nomad.hcl` does.

   Both Traefik instances run with `exposedByDefault: false`, so a service is
   ignored until it carries `traefik.enable=true`.

2. **Pick the right edge.** The two Traefiks share one Consul catalog and split
   it by tag:

   | Edge    | Constraint       | Meaning                                 |
   | ------- | ---------------- | --------------------------------------- |
   | oracle  | ``Tag(`oracle`)``  | only services tagged `oracle`           |
   | homelab | ``!Tag(`oracle`)`` | everything else                         |

   So a homelab service needs no special tag; an Oracle-hosted one must include
   `"oracle"` in its service tags.

3. **Point the hostname at the tunnel.** A proxied CNAME onto the tunnel —
   declarable in code, see below.

## Cloudflare hostnames

Both tunnels authenticate with a token (`CLOUDFLARE_TUNNEL_TOKEN` for the
homelab, `ORACLE_CLOUDFLARE_TUNNEL_TOKEN` for Oracle), which means they are
**remotely managed**: cloudflared pulls its ingress rules from the Cloudflare
dashboard and ignores any local config file.

Both dashboards hold a catch-all rule — everything the tunnel accepts goes to
Traefik, which picks the backend off the `Host` header — so that rule already
covers every current and future route. Adding a hostname is therefore purely a
DNS concern: one proxied CNAME to `<tunnel-id>.cfargotunnel.com`.

> **`bagelindustries.com` resolves every name already.** A proxied wildcard sits
> in front of the tunnel, so a hostname nobody has ever registered still answers
> — with Traefik's own `404 page not found`, served through the tunnel. Checked
> 2026-08-12: `zz-nonexistent-probe-7f3a.bagelindustries.com` behaves exactly
> like `homeassistant.bagelindustries.com` did before it had a route.
>
> Two consequences. A new host in this zone needs **no DNS work at all** to be
> reachable — Consul registration is the whole job. And a 404 on a route that
> should exist means Traefik has no matching router, *never* that DNS is
> missing; checking `dig` will mislead you, because it answers either way.
>
> An explicit record is still worth declaring for a route worth keeping: it is
> more specific than the wildcard, so it documents the hostname where the rest of
> the route lives and survives the wildcard being narrowed or dropped.

Such a record can be declared in code:

```typescript
import { tunnelHostname } from "../../framework/cloudflare-dns";

tunnelHostname(name, {
    domain: "my-service.bagelindustries.com",
    tunnelToken: ctx.cloudflaredTunnelToken,   // or ctx.oracleCfTunnelToken
    provider: ctx.cloudflareProvider,
});
```

The tunnel UUID is not a config value anyone has to keep in sync — a cloudflared
connector token is base64 JSON carrying the account tag, the tunnel id and the
tunnel secret, so `tunnelHostname` decodes the id straight out of the token the
stack already holds.

What that needs is a **Cloudflare API token**, which is a different thing from a
tunnel token: the connector tokens above authenticate cloudflared and carry no
API scope at all. The API token lives in Infisical as `CLOUDFLARE_API_TOKEN` at
secret path `/cloudflare`, and needs Zone:Read + DNS:Edit on
`bagelindustries.com` and `raygen.dev`, plus account-level Access: Apps and
Policies:Edit for Access-protected hostnames. A missing one fails the deploy at that read rather than
leaving a hostname silently unresolvable.

Two caveats:

- **The routes that predate this are still dashboard-managed.** Their records
  were created by the Zero Trust UI and are not in Pulumi's state, so declaring
  one now collides with the existing record instead of adopting it. Import it,
  or delete it in Cloudflare first. (The wildcard above is untouched either way —
  an explicit record simply wins over it for that one name.)
- **A hostname on a tunnel whose ingress lacks a catch-all still needs a public
  hostname entry** in **Zero Trust → Networks → Tunnels** (service
  `http://192.168.0.203:80` for the homelab, `http://localhost:80` for Oracle).
  The CNAME gets the request to the tunnel; the ingress rule is what makes
  cloudflared answer for that host rather than 404.

`/etc/cloudflared/config.yml` on the homelab node carries exactly that catch-all
to `http://192.168.0.203:80`. It is inert for a remotely-managed tunnel and only
takes effect if the tunnel is ever switched to local management.

## Current routes

| Hostname                      | Edge    | Backend                          |
| ----------------------------- | ------- | -------------------------------- |
| `auth.bagelindustries.com`    | homelab | authentik (220:9000)             |
| `demo.bagelindustries.com`    | homelab | `demo` job — Authentik-protected |
| `lookout.raygen.dev`          | homelab | `lookout` job                    |
| `hellonomad.raygen.dev`       | homelab | `hello-world` job (smoke test)   |
| `consul.bagelindustries.com`  | homelab | Consul UI (201:8500) — **open**  |
| `nomad.bagelindustries.com`   | homelab | Nomad UI (202:4646) — **open**   |
| `panel.bagelindustries.com`   | oracle  | `oracle-pelican` job             |
| `homeassistant.bagelindustries.com` | homelab | Home Assistant (200:8123) — **unmanaged host** |
| `proxmox.raygen.dev` | homelab | optiplex (HTTPS 8006) — **Cloudflare Access** |

> **The two cluster UIs are unauthenticated.** Anyone who reaches
> `consul.` or `nomad.` gets a full admin interface — the Nomad UI can submit
> jobs. They were registered that way before the framework refactor and are kept
> as-is here so the routes are not silently dropped, but they should either get
> `authentik@consulcatalog` on their routers (once a matching Authentik provider
> exists) or be taken off the tunnel entirely and reached over NetBird.

## Routing to a host this stack does not create

Traefik reads the Consul catalog, not the Proxmox inventory, so a machine
Pulumi never provisioned can carry a route as long as something registers it.
Modules under `external/` do exactly that — a `register` that creates a
`consul.Node` + `consul.Service` with Traefik tags and no machine at all. They
are auto-discovered like any other service directory.

`external/homeassistant` is the current example: Home Assistant is its own
appliance-style install on `192.168.0.200`, so the module owns the route — the
catalog entry and the DNS record — and nothing else. Two things about that host
are outside Pulumi's reach and have to be true for the route to work:

- **The address must be pinned.** `.200` sits at the top of the Eero's DHCP
  pool, so Home Assistant needs a reservation (or a static address on the host)
  or the catalog entry ends up pointing at whatever leases it next.
- **Home Assistant must trust the proxy.** It rejects proxied requests with a
  400 until `configuration.yaml` carries

  ```yaml
  http:
    use_x_forwarded_for: true
    trusted_proxies:
      - 192.168.0.203   # traefik
  ```

  and it is restarted.

The route is unauthenticated at the edge on purpose: Home Assistant does its own
auth, and the companion apps and token-based integrations cannot complete an
Authentik login, so a forwardauth in front would lock them out.

## Authentik-protected routes

Setting `protected: true` on a `reverseProxy` (or adding
`traefik.http.routers.<n>.middlewares=authentik@consulcatalog` to a Nomad
service) sends the request through Authentik's embedded outpost first.

The `authentik@consulcatalog` middleware and the
`PathPrefix(/outpost.goauthentik.io/)` router that serves the login callback are
both defined as Consul tags on the Authentik service itself
(`lxcs/authentik/index.ts`). The outpost router runs at priority 100 so it wins
over the protected host's own rule for that path, on every host.

Note that a protected route needs a matching provider configured inside
Authentik; the middleware only forwards the auth check.

## Cloudflare Access-protected routes

`tunnelHostname()` accepts `accessEmails: ["raygenrrupe@gmail.com"]` to create
a self-hosted Access application for that exact hostname. It uses the zone's
account and existing login methods, an eight-hour session, and a single Allow
policy containing only the listed email addresses. All other identities are
denied; there is no bypass policy. Omitting `accessEmails` keeps a route public;
an empty list is rejected. This is separate from the Authentik `protected` flag.

`external/proxmox` uses this for `proxmox.raygen.dev`. Its Consul service waits
for both the Access-backed DNS record and the Traefik transport before becoming
routable, even if wildcard DNS already resolves the hostname. The backend address
comes from the **LAN** `PROXMOX_ENDPOINT` config (required in CI too), never the
CI-only NetBird endpoint, and the backend port is HTTPS 8006. Proxmox still
requires its own login after Cloudflare Access.

Only this route opts into `proxmox@file`, a Traefik transport that accepts the
host's self-signed certificate. Traffic remains encrypted, but this hop does
not authenticate the origin certificate; other routes retain TLS verification.
Replace this exception with a trusted Proxmox CA/certificate when available.
The router also allows only the cloudflared socket peer (`192.168.0.204`), not
client-supplied forwarded headers, so direct LAN requests to Traefik cannot
bypass Access. Direct LAN access to Proxmox itself remains unchanged.

Before deploying:

- Ensure the Cloudflare token has the permissions above, and that the account
  has a working Access login method (for example Google or one-time PIN).
- Import any existing explicit `proxmox.raygen.dev` DNS record or matching
  Access application into Pulumi rather than trying to create a duplicate.
- Run `pnpm test` and a refreshed `pulumi preview`. The playbook update can
  reprovision Hashistack services; review its command changes before applying.

After deployment, verify an unauthenticated browser is sent to Access, only
`raygenrrupe@gmail.com` can proceed to Proxmox, and a Proxmox console WebSocket
works after login. A direct request to Traefik with `Host: proxmox.raygen.dev`
must return 403, including with a forged `X-Forwarded-For: 192.168.0.204`.
For rollback, remove the Consul route first and verify it no longer serves
Proxmox **before** removing Access or DNS; wildcard DNS may still resolve it.

## Forwarded headers

Both Traefiks trust `X-Forwarded-*` from their tunnel — the homelab by
`trustedIPs` (the cloudflared LXC only), Oracle by
`forwardedHeaders.insecure` (cloudflared shares the host's network namespace, so
there is no stable peer address to list).

Without this, backends see `X-Forwarded-Proto: http` and anything that builds
absolute URLs — Pelican with `BEHIND_PROXY=true`, Authentik's redirects — either
emits `http://` links or redirect-loops.
