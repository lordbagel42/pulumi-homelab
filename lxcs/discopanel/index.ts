import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import { sshSetup } from "../../utils/ssh";
import { InfisicalConfig, lxcPassword } from "../../infisical";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../hashistack";
import { ip, GATEWAY } from "../../framework";
import type { ServiceContext } from "../../framework";

const DISCOPANEL_VMID = 212;
const DISCOPANEL_IP = ip(DISCOPANEL_VMID);

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "discopanel";
export const provides = ["discopanel-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): void {
    createDiscopanel({
        provider: ctx.provider,
        infisicalConfig: ctx.infisicalConfig,
        sshKey: ctx.sshKey,
        sshPrivateKey: ctx.sshPrivateKey,
        consulIp: CONSUL_IP_CONST,
        consulVersion: CONSUL_VERSION,
    }, cmd => ctx.commands.set("discopanel-setup", cmd));
}

// ── Legacy interface ───────────────────────────────────────────────────────────
export interface DiscopanelArgs {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    consulIp: string;
    consulVersion: string;
}

export function createDiscopanel(
    { provider, infisicalConfig, sshKey, sshPrivateKey, consulIp, consulVersion }: DiscopanelArgs,
    onSetup?: (cmd: import("@pulumi/command").local.Command) => void,
): void {
    const scriptPath = path.join(__dirname, "discopanel-setup.sh");

    const container = new proxmox.ContainerLegacy("discopanel", {
        nodeName: "optiplex",
        vmId: DISCOPANEL_VMID,
        cpu: { cores: 6 },
        memory: { dedicated: 14336 },
        disk: { datastoreId: "local", size: 48 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "discopanel",
            ipConfigs: [{ ipv4: { address: `${DISCOPANEL_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("discopanel", infisicalConfig), keys: [sshKey] },
        },
        features: { nesting: true },
        unprivileged: false,
        tags: ["discopanel", "app", "minecraft"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const setupCmd = sshSetup("discopanel-setup", DISCOPANEL_IP, scriptPath, {
        DISCOPANEL_IP,
        CONSUL_IP: consulIp,
        CONSUL_VERSION: consulVersion,
    }, sshPrivateKey, [container]);
    onSetup?.(setupCmd);
}
