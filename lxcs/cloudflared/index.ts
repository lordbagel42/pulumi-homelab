import * as path from "path";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
//
// The homelab's own Cloudflare tunnel connector.
//
// CT 204 previously existed only as a hand-made container: nothing here created
// it, nothing provisioned it, and CLOUDFLARE_TUNNEL_TOKEN was read in index.ts
// and then never consumed by anything. It failed `vzstart` for ~35 days with an
// LXC pre-start hook failure (its local-lvm rootfs volume had gone), and because
// it was unmanaged no deploy ever noticed or repaired it.
//
// This is a separate tunnel from Oracle's: that one runs as the oracle-cloudflared
// nomad job off ORACLE_CLOUDFLARE_TUNNEL_TOKEN, in datacenter "oracle". This
// connector fronts the homelab side, so both can be up at once.
export const name = "cloudflared";
export const provides = ["cloudflared-setup"];
export const dependencies: string[] = [];

const CLOUDFLARED_VMID = 204;
const CLOUDFLARED_IP = ip(CLOUDFLARED_VMID);

export function register(ctx: ServiceContext): void {
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: CLOUDFLARED_VMID,
        ip: CLOUDFLARED_IP,
        cpu: 2,
        memory: 1024,
        // The old container was 2G, which is tight once apt lists are populated.
        disk: 4,
        tags: ["system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const provision = ansibleProvision("cloudflared-provision", {
        host: CLOUDFLARED_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        // Resolved through pulumi.output() in ansibleProvision, so the secret
        // arrives as its real value and travels via env + a 0600 file rather
        // than the command line.
        extraVars: { tunnel_token: ctx.cloudflaredTunnelToken },
        dependsOn: [machine],
    });

    ctx.commands.set("cloudflared-setup", provision);
}
