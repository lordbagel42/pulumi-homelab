import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import { sshSetup } from "../../utils/ssh";
import { InfisicalConfig, lxcPassword } from "../../infisical";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../hashistack";
import { ip, GATEWAY } from "../../framework";
import type { ServiceContext } from "../../framework";

const DOKPLOY_VMID = 211;
const DOKPLOY_IP = ip(DOKPLOY_VMID);

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "dokploy";
export const provides = ["dokploy-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): void {
    createDokploy({
        provider: ctx.provider,
        infisicalConfig: ctx.infisicalConfig,
        sshKey: ctx.sshKey,
        sshPrivateKey: ctx.sshPrivateKey,
        consulIp: CONSUL_IP_CONST,
        consulVersion: CONSUL_VERSION,
    }, cmd => ctx.commands.set("dokploy-setup", cmd));
}

// ── Legacy interface ───────────────────────────────────────────────────────────
export interface DokployArgs {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    consulIp: string;
    consulVersion: string;
}

export function createDokploy(
    { provider, infisicalConfig, sshKey, sshPrivateKey, consulIp, consulVersion }: DokployArgs,
    onSetup?: (cmd: import("@pulumi/command").local.Command) => void,
): void {
    const dokployScriptPath = path.join(__dirname, "dokploy-setup.sh");

    const dokployContainer = new proxmox.ContainerLegacy("dokploy", {
        nodeName: "optiplex",
        vmId: DOKPLOY_VMID,
        cpu: { cores: 4 },
        memory: { dedicated: 10240 },
        disk: { datastoreId: "local-lvm", size: 24 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "dokploy",
            ipConfigs: [{ ipv4: { address: `${DOKPLOY_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("dokploy", infisicalConfig), keys: [sshKey] },
        },
        features: { nesting: true },
        unprivileged: false,
        tags: ["dokploy", "app"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const setupCmd = sshSetup("dokploy-setup", DOKPLOY_IP, dokployScriptPath, {
        CONSUL_VERSION: consulVersion,
        CONSUL_IP: consulIp,
        DOKPLOY_IP,
    }, sshPrivateKey, [dokployContainer]);
    onSetup?.(setupCmd);
}
