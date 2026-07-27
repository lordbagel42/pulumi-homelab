# Poke Sandbox VM

An **optional** dedicated Proxmox VM that serves as a persistent sandbox for
[Poke](https://poke.com) to run code and stateful tools, hosting long-lived MCP
bridge servers (Claude Code and other stateful/"dangerous" tools) behind an
API-key-authenticated gateway.

Implements issues #9 (provision the VM) and #10 (set up Claude Code & stateful
MCP tools).

## What it provisions

| Resource         | Value                          |
| ---------------- | ------------------------------ |
| VM ID / IP       | `231` / `192.168.0.231`        |
| CPU / RAM / disk | 4 cores / 8 GiB / 40 GiB       |
| OS               | Debian 12 (cloud image)        |
| Sandbox user     | `poke` (in the `docker` group) |
| Workspace        | `/home/poke/workspace`         |

Installed tooling: Node.js 22, Docker Engine, Claude Code
(`@anthropic-ai/claude-code`), and [`supergateway`](https://github.com/supercorp-ai/supergateway)
to bridge stdio MCP servers to SSE over the network.

## Architecture

```
Poke ──API key──▶ mcp-auth-gateway (:8080, better-auth)
                      │  validates x-api-key / Bearer, then proxies
                      ├─ /claude-code/*  ─▶ 127.0.0.1:9100  (claude mcp serve)
                      └─ /filesystem/*   ─▶ 127.0.0.1:9101  (filesystem MCP)
```

Only port **8080** (the gateway) and **22** (SSH) are reachable from the
network. The raw bridges on `9100`/`9101` bind all interfaces (supergateway has
no bind-address flag) and have no auth of their own, so they are kept off the
network by `ufw` alone: default-deny incoming plus explicit deny rules for those
two ports. The auth gateway is the only intended entrypoint.

Clients (Poke) connect to the SSE endpoint under the relevant prefix:

- Claude Code: `<public-url>/claude-code/sse`
- Filesystem:  `<public-url>/filesystem/sse`

sending the API key on every request (`x-api-key` or `Authorization: Bearer`).

## Authentication (better-auth)

The gateway is a small Node app (`vms/sandbox/mcp-auth-gateway/`) built on
[better-auth](https://www.better-auth.com) using its **API-key** plugin:

- Every request to a `/claude-code/*` or `/filesystem/*` path must carry a key,
  as either `x-api-key: <key>` or `Authorization: Bearer <key>`. Anything else
  gets a `401` with a `WWW-Authenticate` header.
- Keys are stored **hashed** in a local SQLite database
  (`/var/lib/mcp-auth/auth.db`); better-auth handles verification, and can also
  do expiry/rate-limiting/revocation.
- better-auth's HTTP endpoints (`/api/auth/*`) are **not** exposed. Key
  management happens in-process only (via the seed script over SSH), so there is
  no public route that can mint a key.

On first provision a single key (name `poke-sandbox`) is generated and its
plaintext is written to a `poke`-owned `0600` file on the VM. Fetch it once and
give it to Poke:

```bash
ssh poke@192.168.0.231 cat /var/lib/mcp-auth/mcp-api-key
```

To rotate: delete `/var/lib/mcp-auth/mcp-api-key`, revoke the old key in the DB,
and re-run the seed script (`cd /opt/mcp-auth-gateway && node seed.mjs` with the
gateway env sourced).

## Deployment

The module always deploys. There is no feature flag and nothing to set before a
deploy will create the VM.

Two optional overrides, both with working defaults:

```bash
# URL Poke reaches the gateway on (default http://192.168.0.231:8080)
pulumi config set SANDBOX_PUBLIC_URL https://sandbox.example.com
# gateway admin account (default poke@sandbox.local)
pulumi config set SANDBOX_ADMIN_EMAIL you@example.com
```

`ANTHROPIC_API_KEY` in Infisical under `/sandbox` is **optional**. Without it the
VM, the auth gateway and the filesystem bridge all come up normally and only
`claude-code-mcp` stays down; the deploy logs a warning rather than failing. Add
the key to Infisical whenever you like and the next deploy picks it up — no code
change needed.

Pulumi generates the better-auth secret and the gateway admin password
automatically (persisted in Infisical) and injects them, along with the
Anthropic key when present, into `/etc/sandbox/*.env` over SSH — none of these
are passed through Ansible extra-vars, which cannot carry a secret.

## Security notes

- The gateway is the single network entrypoint and is API-key protected, but it
  has no TLS of its own. When exposing it beyond the LAN, front it with an
  authenticated/encrypted path (Cloudflare tunnel, NetBird, or a TLS reverse
  proxy) and set `SANDBOX_PUBLIC_URL` accordingly.
- The sandbox is intentionally permissive (Docker + Claude Code) by design — the
  API key is what keeps it from being open to the world.

## Alternative auth model

If your MCP client can complete an interactive OAuth 2.1 flow, better-auth's
`mcp` plugin (`withMcpAuth` + dynamic client registration) can replace the
API-key gateway for a fully spec-compliant setup. The API-key model here was
chosen because it suits a headless client like Poke.
