import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { nodeLxcTemplate } from "../../framework/node-assets";
import { ansibleProvision } from "../../utils/ansible";
import { optionalSecret, managedSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
//
// Issue 9:  provision a dedicated, persistent sandbox in Proxmox for Poke to
//           run code and stateful tools.
// Issue 10: configure it to host persistent MCP bridge servers — Claude Code
//           and other stateful/\"dangerous\" tools — reachable over the network.
//
// The raw MCP bridges are protected by a better-auth gateway (API-key auth) that
// is the only network-facing port; the bridges themselves are firewalled off.
//
// This module always deploys — no config flag, no required secret. Everything
// it needs beyond the shared SSH key and VM password is either generated and
// persisted automatically (the better-auth secret and gateway admin password)
// or optional (ANTHROPIC_API_KEY at /sandbox in Infisical, which only
// claude-code-mcp needs).
//
// Optional overrides, both with working defaults:
//   pulumi config set SANDBOX_PUBLIC_URL   https://sandbox.example.com
//   pulumi config set SANDBOX_ADMIN_EMAIL  you@example.com
export const name = "sandbox";
export const provides = ["sandbox-setup", "sandbox-mcp"];
export const dependencies: string[] = [];

// Static guest in the 230–239 range (rule: vmId = last IP octet).
// 230 is reserved for nomad-client, so the sandbox takes 231.
const SANDBOX_VMID = 231;
const SANDBOX_IP = ip(SANDBOX_VMID);

// Hosted on tower rather than the default optiplex — it has the headroom.
const SANDBOX_NODE = "tower";

// Only the gateway is exposed; the bridges listen on internal, firewalled ports.
const GATEWAY_PORT = 8080;

const SANDBOX_USER = "poke";
const WORKSPACE_DIR = `/home/${SANDBOX_USER}/workspace`;

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config();

    // Public URL Poke reaches the gateway on. Defaults to the LAN address; set
    // this to the tunnel/reverse-proxy URL when exposing the sandbox externally.
    const publicUrl = config.get("SANDBOX_PUBLIC_URL") ?? `http://${SANDBOX_IP}:${GATEWAY_PORT}`;
    const adminEmail = config.get("SANDBOX_ADMIN_EMAIL") ?? "poke@sandbox.local";

    // ── Container (issue 9) ─────────────────────────────────────────────────────
    // An LXC, not a VM: tower refuses to start KVM guests ("KVM virtualisation
    // configured, but not available"), and the Proxmox provider exposes no way to
    // turn KVM off per-VM. A container needs no hardware virtualisation at all, and
    // nesting + privileged is the same shape dokploy already uses to run Docker.
    //
    // The template lives on node-local `local` storage, so tower needs its own copy
    // — the default `local:vztmpl/...` id only resolves on optiplex.
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        nodeName: SANDBOX_NODE,
        templateFileId: nodeLxcTemplate(SANDBOX_NODE, ctx.provider),
        vmId: SANDBOX_VMID,
        ip: SANDBOX_IP,
        // Roomy enough to build code and run containerised tools.
        cpu: 4,
        memory: 12288,
        disk: 40,
        nesting: true,
        privileged: true,
        tags: ["sandbox", "ai"],
        sshKeys: [ctx.sshKey],
        password: ctx.vmPassword,
    }, { provider: ctx.provider });

    // ── Tooling + MCP bridge + auth gateway (issue 10) ───────────────────────────
    // The playbook installs Node/Docker/Claude Code, the better-auth gateway and
    // the systemd services. Secrets are NOT passed here: ansibleProvision
    // JSON-encodes its extraVars, which cannot carry a pulumi.Output secret —
    // they are injected out-of-band below.
    const provision = ansibleProvision("sandbox-provision", {
        host: SANDBOX_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            sandbox_user: SANDBOX_USER,
            workspace_dir: WORKSPACE_DIR,
            gateway_port: GATEWAY_PORT,
            public_url: publicUrl,
        },
        dependsOn: [machine],
    });
    ctx.commands.set("sandbox-setup", provision);

    // ── Inject secrets + seed the API key (issue 10) ─────────────────────────────
    // Written over SSH so the secret Outputs resolve properly (extraVars can't
    // carry them). Secrets travel through the command environment and are piped
    // to remote files via stdin, so they never land in plaintext in Pulumi state
    // nor in a remote process's argv.
    // Optional, not required: readSecret aborts the whole update when a key is
    // absent, so a missing ANTHROPIC_API_KEY used to take down every unrelated
    // resource in the stack. The sandbox, the gateway and the filesystem bridge all
    // come up without it; only Claude Code needs it, and it can be added to
    // Infisical at /sandbox later without touching this code.
    const anthropicApiKey = optionalSecret("ANTHROPIC_API_KEY", {
        ...ctx.infisicalConfig,
        secretPath: "/sandbox",
    });
    // Auto-generated and persisted in Infisical so they stay stable across runs.
    const betterAuthSecret = managedSecret("sandbox-better-auth-secret", ctx.infisicalConfig);
    const adminPassword = managedSecret("sandbox-gateway-admin-password", ctx.infisicalConfig);

    const credentials = new command.local.Command("sandbox-mcp-credentials", {
        interpreter: ["/bin/bash", "-c"],
        create: `
set -euo pipefail
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
trap 'rm -f "$key"' EXIT
SSH="ssh -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10"
HOST=root@${SANDBOX_IP}

$SSH "$HOST" "install -d -m 0750 /etc/sandbox"

printf 'ANTHROPIC_API_KEY=%s\n' "$_ANTHROPIC_API_KEY" | \\
  $SSH "$HOST" "umask 077 && cat > /etc/sandbox/mcp.env"

{
  printf 'BETTER_AUTH_SECRET=%s\n' "$_BETTER_AUTH_SECRET"
  printf 'BETTER_AUTH_URL=%s\n' "$_PUBLIC_URL"
  printf 'GATEWAY_ADMIN_EMAIL=%s\n' "$_ADMIN_EMAIL"
  printf 'GATEWAY_ADMIN_PASSWORD=%s\n' "$_ADMIN_PASSWORD"
  printf 'API_KEY_FILE=%s\n' "/var/lib/mcp-auth/mcp-api-key"
} | $SSH "$HOST" "umask 077 && cat > /etc/sandbox/gateway.env"

# Restart so the gateway picks up the injected secret, then seed the API key.
# seed runs with '&&' so a seeding failure fails the whole resource (no silent success).
$SSH "$HOST" "set -a; . /etc/sandbox/gateway.env; set +a; export AUTH_DB=/var/lib/mcp-auth/auth.db HOME=/home/${SANDBOX_USER}; cd /opt/mcp-auth-gateway; systemctl restart mcp-auth-gateway filesystem-mcp && sudo -u ${SANDBOX_USER} -E node seed.mjs"

# claude-code-mcp is the one unit that genuinely needs ANTHROPIC_API_KEY. Keep it
# out of the '&&' above so an unset key leaves the sandbox and its gateway fully
# deployed instead of failing the update.
if ! $SSH "$HOST" "systemctl restart claude-code-mcp"; then
  echo "warning: claude-code-mcp did not start — set ANTHROPIC_API_KEY in Infisical at /sandbox" >&2
fi
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _ANTHROPIC_API_KEY: anthropicApiKey,
            _BETTER_AUTH_SECRET: betterAuthSecret,
            _ADMIN_PASSWORD: adminPassword,
            _ADMIN_EMAIL: adminEmail,
            _PUBLIC_URL: publicUrl,
        },
    }, { dependsOn: [provision] });
    ctx.commands.set("sandbox-mcp", credentials);
}
