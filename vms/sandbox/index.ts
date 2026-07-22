import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { managedSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
//
// Issue 9:  provision a dedicated, persistent sandbox VM in Proxmox for Poke to
//           run code and stateful tools.
// Issue 10: configure that VM to host persistent MCP bridge servers — Claude Code
//           and other stateful/"dangerous" tools — reachable over the network.
//
// The raw MCP bridges are protected by a better-auth gateway (API-key auth) that
// is the only network-facing port; the bridges themselves are firewalled off.
//
// Claude Code authenticates via OAuth, done manually on the VM (`claude` login as
// the sandbox user) — no API key is provisioned here.
export const name = "sandbox";
export const provides = ["sandbox-setup", "sandbox-mcp"];
export const dependencies: string[] = [];

// Static VM in the 230–239 range (rule: vmId = last IP octet).
// 230 is reserved for nomad-client, so the sandbox takes 231.
const SANDBOX_VMID = 231;
const SANDBOX_IP = ip(SANDBOX_VMID);

// Only the gateway is exposed; the bridges listen on internal, firewalled ports.
const GATEWAY_PORT = 8080;

const SANDBOX_USER = "poke";
const WORKSPACE_DIR = `/home/${SANDBOX_USER}/workspace`;
const ADMIN_EMAIL = "poke@sandbox.local";

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config();

    // Public URL Poke reaches the gateway on. Defaults to the LAN address; set
    // this to the tunnel/reverse-proxy URL when exposing the sandbox externally.
    const publicUrl = config.get("SANDBOX_PUBLIC_URL") ?? `http://${SANDBOX_IP}:${GATEWAY_PORT}`;

    // ── VM (issue 9) ────────────────────────────────────────────────────────────
    // Roomy enough to build code and run containerised tools.
    const machine = new ProxmoxMachine(name, {
        type: "vm",
        vmId: SANDBOX_VMID,
        ip: SANDBOX_IP,
        cpu: 4,
        memory: 8192,
        disk: 40,
        importFrom: ctx.debianCloudImageId,
        userDataFileId: ctx.cloudInitSnippetId,
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

    // ── Inject the gateway secret + seed the API key (issue 10) ──────────────────
    // Written over SSH so the secret Outputs resolve properly (extraVars can't
    // carry them). Secrets travel through the command environment and are piped
    // to remote files via stdin, so they never land in plaintext in Pulumi state
    // nor in a remote process's argv. Both are auto-generated and persisted in
    // Infisical so they stay stable across runs — nothing to set up by hand.
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

{
  printf 'BETTER_AUTH_SECRET=%s\n' "$_BETTER_AUTH_SECRET"
  printf 'BETTER_AUTH_URL=%s\n' "$_PUBLIC_URL"
  printf 'GATEWAY_ADMIN_EMAIL=%s\n' "$_ADMIN_EMAIL"
  printf 'GATEWAY_ADMIN_PASSWORD=%s\n' "$_ADMIN_PASSWORD"
  printf 'API_KEY_FILE=%s\n' "/var/lib/mcp-auth/mcp-api-key"
} | $SSH "$HOST" "umask 077 && cat > /etc/sandbox/gateway.env"

# Restart so the gateway picks up the injected secret, then seed the API key.
# seed runs with '&&' so a seeding failure fails the whole resource (no silent success).
$SSH "$HOST" "set -a; . /etc/sandbox/gateway.env; set +a; export AUTH_DB=/var/lib/mcp-auth/auth.db HOME=/home/${SANDBOX_USER}; cd /opt/mcp-auth-gateway; systemctl restart mcp-auth-gateway && sudo -u ${SANDBOX_USER} -E node seed.mjs"
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _BETTER_AUTH_SECRET: betterAuthSecret,
            _ADMIN_PASSWORD: adminPassword,
            _ADMIN_EMAIL: ADMIN_EMAIL,
            _PUBLIC_URL: publicUrl,
        },
    }, { dependsOn: [provision] });
    ctx.commands.set("sandbox-mcp", credentials);
}
