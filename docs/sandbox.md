# Poke Sandbox VM

An **optional** dedicated Proxmox VM that serves as a persistent sandbox for
[Poke](https://poke.com) to run code and stateful tools, hosting long-lived MCP
bridge servers (Claude Code and other stateful/"dangerous" tools).

Implements issues #9 (provision the VM) and #10 (set up Claude Code & stateful
MCP tools).

## What it provisions

| Resource        | Value                          |
| --------------- | ------------------------------ |
| VM ID / IP      | `231` / `192.168.0.231`        |
| CPU / RAM / disk| 4 cores / 8 GiB / 40 GiB       |
| OS              | Debian 12 (cloud image)        |
| Sandbox user    | `poke` (in the `docker` group) |
| Workspace       | `/home/poke/workspace`         |

Installed tooling: Node.js 22, Docker Engine, Claude Code
(`@anthropic-ai/claude-code`), and [`supergateway`](https://github.com/supercorp-ai/supergateway)
to bridge stdio MCP servers to SSE over the network.

## Persistent MCP bridge servers

Each server runs under systemd (`Restart=always`) and is exposed over SSE by
`supergateway` so a remote client (Poke) can reach a long-lived, stateful
process:

| Service              | Port | Endpoint                | Backing MCP server                      |
| -------------------- | ---- | ----------------------- | --------------------------------------- |
| `claude-code-mcp`    | 8100 | `http://<ip>:8100/sse`  | `claude mcp serve`                      |
| `filesystem-mcp`     | 8101 | `http://<ip>:8101/sse`  | `@modelcontextprotocol/server-filesystem` |

## Enabling it

The module is a no-op unless the stack opts in:

```bash
pulumi config set SANDBOX_ENABLED true
```

It also expects an `ANTHROPIC_API_KEY` secret in Infisical under the `/sandbox`
path. Pulumi injects that key into `/etc/sandbox/mcp.env` on the VM over SSH
(it is not passed through Ansible extra-vars, which cannot carry a secret) and
restarts the MCP services.

## Security note

The MCP bridges listen on all interfaces without their own authentication, and
the sandbox is intentionally permissive (Docker, Claude Code). Only expose the
ports to Poke through an authenticated path (e.g. a Cloudflare tunnel or
NetBird), never directly to the public internet.
