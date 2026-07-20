import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { readSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
//
// Issue 9:  provision a dedicated, persistent sandbox VM in Proxmox for Poke to
//           run code and stateful tools.
// Issue 10: configure that VM to host persistent MCP bridge servers — Claude Code
//           and other stateful/"dangerous" tools — reachable over the network.
//
// The module is OPTIONAL. It only provisions anything when the stack opts in:
//
//   pulumi config set SANDBOX_ENABLED true
//
// and expects an `ANTHROPIC_API_KEY` secret to exist in Infisical under /sandbox.
export const name = "sandbox";
export const provides = ["sandbox-setup", "sandbox-mcp"];
export const dependencies: string[] = [];

// Static VM in the 230–239 range (rule: vmId = last IP octet).
// 230 is reserved for nomad-client, so the sandbox takes 231.
const SANDBOX_VMID = 231;
const SANDBOX_IP = ip(SANDBOX_VMID);

// Persistent MCP bridge servers listen on these ports (SSE over HTTP).
const CLAUDE_MCP_PORT = 8100;
const FILESYSTEM_MCP_PORT = 8101;

const SANDBOX_USER = "poke";
const WORKSPACE_DIR = `/home/${SANDBOX_USER}/workspace`;

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config();
    if (!config.getBoolean("SANDBOX_ENABLED")) {
        // Not opted in — do nothing so the module stays a no-op on most stacks.
        return;
    }

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

    // ── Tooling + MCP bridge services (issue 10) ─────────────────────────────────
    // The playbook installs Node/Docker/Claude Code and stands up the systemd
    // services. Secrets are NOT passed here: ansibleProvision JSON-encodes its
    // extraVars, which cannot carry a pulumi.Output secret — the key is injected
    // out-of-band below.
    const provision = ansibleProvision("sandbox-provision", {
        host: SANDBOX_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            sandbox_user: SANDBOX_USER,
            workspace_dir: WORKSPACE_DIR,
            claude_mcp_port: CLAUDE_MCP_PORT,
            filesystem_mcp_port: FILESYSTEM_MCP_PORT,
        },
        dependsOn: [machine],
    });
    ctx.commands.set("sandbox-setup", provision);

    // ── Inject the Anthropic API key (issue 10) ──────────────────────────────────
    // Written straight to the MCP env file over SSH so the secret Output is
    // resolved properly (extraVars can't carry it). The key is passed via the
    // command environment, never interpolated into the script body, so it does
    // not land in plaintext in Pulumi state.
    const anthropicApiKey = readSecret("ANTHROPIC_API_KEY", {
        ...ctx.infisicalConfig,
        secretPath: "/sandbox",
    });

    const credentials = new command.local.Command("sandbox-mcp-credentials", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${SANDBOX_IP} \\
  "install -d -m 0750 /etc/sandbox && umask 077 && printf 'ANTHROPIC_API_KEY=%s\\n' \\"$_ANTHROPIC_API_KEY\\" > /etc/sandbox/mcp.env && systemctl restart claude-code-mcp filesystem-mcp"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _ANTHROPIC_API_KEY: anthropicApiKey,
        },
    }, { dependsOn: [provision] });
    ctx.commands.set("sandbox-mcp", credentials);
}
