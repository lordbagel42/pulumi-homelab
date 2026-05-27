import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as consul from "@pulumi/consul";
import { sshSetup } from "../../utils/ssh";
import { lxcPassword } from "../../infisical";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../hashistack";
import { ip, GATEWAY } from "../../framework";
import type { ServiceContext } from "../../framework";
import { installAlloy } from "../../utils/alloy";

const UPTIME_KUMA_VMID = 212;
const UPTIME_KUMA_IP = ip(UPTIME_KUMA_VMID);

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "uptime-kuma";
export const provides = ["uptime-kuma-setup"];
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const scriptPath = path.join(__dirname, "uptime-kuma-setup.sh");

    const container = new proxmox.ContainerLegacy("uptime-kuma", {
        nodeName: "optiplex",
        vmId: UPTIME_KUMA_VMID,
        cpu: { cores: 2 },
        memory: { dedicated: 2048 },
        disk: { datastoreId: "local-lvm", size: 8 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "uptime-kuma",
            ipConfigs: [{ ipv4: { address: `${UPTIME_KUMA_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("uptime-kuma", ctx.infisicalConfig), keys: [ctx.sshKey] },
        },
        features: { nesting: true },
        unprivileged: false,
        tags: ["monitoring", "app"],
        startOnBoot: true,
        started: true,
    }, { provider: ctx.provider });

    const setupCmd = sshSetup(
        "uptime-kuma-setup",
        UPTIME_KUMA_IP,
        scriptPath,
        {
            CONSUL_VERSION,
            CONSUL_IP: CONSUL_IP_CONST,
            UPTIME_KUMA_IP,
        },
        ctx.sshPrivateKey,
        [container],
    );
    ctx.commands.set("uptime-kuma-setup", setupCmd);

    // ── Consul service registration ────────────────────────────────────────────
    const consulNode = ctx.commands.get("consul-node");
    const consulProvider = new consul.Provider("uptime-kuma-consul", {
        address: `${CONSUL_IP_CONST}:8500`,
        scheme: "http",
    }, { dependsOn: [setupCmd] });

    new consul.Service("uptime-kuma-consul-service", {
        name: "uptime-kuma",
        node: "pulumi-services",
        address: UPTIME_KUMA_IP,
        port: 3001,
        tags: [
            "traefik.enable=true",
            "traefik.http.routers.uptime-kuma.rule=Host(`uptime.bagelindustries.com`)",
            "traefik.http.routers.uptime-kuma.entrypoints=web",
            "traefik.http.routers.uptime-kuma.middlewares=authentik@consulcatalog",
            "traefik.http.services.uptime-kuma.loadbalancer.server.port=3001",
        ],
    }, { provider: consulProvider, dependsOn: consulNode ? [consulNode] : [] });

    if (ctx.grafana) {
        installAlloy("uptime-kuma", UPTIME_KUMA_IP, "uptime-kuma", ctx.grafana, ctx.sshPrivateKey, [setupCmd]);
    }
}
