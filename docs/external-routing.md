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

3. **Point the hostname at the tunnel.** This is the one step Pulumi does not
   do — see below.

## The manual step: Cloudflare hostnames

Both tunnels authenticate with a token (`CLOUDFLARE_TUNNEL_TOKEN` for the
homelab, `ORACLE_CLOUDFLARE_TUNNEL_TOKEN` for Oracle), which means they are
**remotely managed**: cloudflared pulls its ingress rules from the Cloudflare
dashboard and ignores any local config file. Pulumi has no Cloudflare API
credentials, so it cannot create those rules.

For each new hostname, add a public hostname to the tunnel in
**Cloudflare Zero Trust → Networks → Tunnels**:

| Field    | Homelab                  | Oracle              |
| -------- | ------------------------ | ------------------- |
| Service  | `http://192.168.0.203:80` | `http://localhost:80` |
| Hostname | the service's domain     | the service's domain |

Cloudflare creates the proxied DNS record automatically. Because Traefik picks
the backend from the `Host` header, a single catch-all rule per tunnel is enough
to cover every current and future route — adding a hostname is then purely a DNS
concern.

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
appliance-style install on `192.168.0.200`, so the module owns the route and
nothing else. Two things about that host are outside Pulumi's reach and have to
be true for the route to work:

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

## Forwarded headers

Both Traefiks trust `X-Forwarded-*` from their tunnel — the homelab by
`trustedIPs` (the cloudflared LXC only), Oracle by
`forwardedHeaders.insecure` (cloudflared shares the host's network namespace, so
there is no stable peer address to list).

Without this, backends see `X-Forwarded-Proto: http` and anything that builds
absolute URLs — Pelican with `BEHIND_PROXY=true`, Authentik's redirects — either
emits `http://` links or redirect-loops.
