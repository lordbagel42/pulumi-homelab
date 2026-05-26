import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";
import * as consul from "@pulumi/consul";
import { InfisicalConfig, lxcPassword } from "../../infisical";
import { sshSetup as sshSetupUtil } from "../../utils/ssh";
import { ip, GATEWAY, CONSUL_VERSION as CONSUL_VER, NOMAD_VERSION as NOMAD_VER, TRAEFIK_VERSION as TRAEFIK_VER } from "../../framework";
import type { ServiceContext } from "../../framework";
import { installAlloy } from "../../utils/alloy";

// Re-export for backward compat (other modules import these from here)
export const CONSUL_VERSION = CONSUL_VER;
export { sshSetupUtil as sshSetup };

const NOMAD_VERSION = NOMAD_VER;
const TRAEFIK_VERSION = TRAEFIK_VER;

const CONSUL_VMID = 201;
const NOMAD_VMID = 202;
const TRAEFIK_VMID = 203;
const CLOUDFLARED_VMID = 204;
const NOMAD_CLIENT_VMID = 230;

const CONSUL_IP = ip(CONSUL_VMID);
export const CONSUL_IP_CONST = CONSUL_IP;
export const NOMAD_IP = ip(NOMAD_VMID);
const TRAEFIK_IP = ip(TRAEFIK_VMID);
const CLOUDFLARED_IP = ip(CLOUDFLARED_VMID);
const NOMAD_CLIENT_IP = ip(NOMAD_CLIENT_VMID);

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "hashistack";
export const provides = ["consul-setup", "nomad-setup", "nomad-client-setup", "traefik-setup", "cloudflared-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): void {
    createHashistack({
        provider: ctx.provider,
        infisicalConfig: ctx.infisicalConfig,
        sshKey: ctx.sshKey,
        sshPrivateKey: ctx.sshPrivateKey,
        debianCloudImageId: ctx.debianCloudImageId,
        cloudInitSnippetId: ctx.cloudInitSnippetId,
        cloudflaredTunnelToken: ctx.cloudflaredTunnelToken,
    }, ctx.commands);

    if (!ctx.grafana) return;
    const g = ctx.grafana;
    const key = ctx.sshPrivateKey;

    installAlloy("consul",        CONSUL_IP,       "consul",        g, key, [ctx.commands.get("consul-setup")!]);
    installAlloy("nomad-server",  NOMAD_IP,        "nomad-server",  g, key, [ctx.commands.get("nomad-setup")!]);
    installAlloy("traefik",       TRAEFIK_IP,      "traefik",       g, key, [ctx.commands.get("traefik-setup")!]);
    installAlloy("cloudflared",   CLOUDFLARED_IP,  "cloudflared",   g, key, [ctx.commands.get("cloudflared-setup")!]);
    installAlloy("nomad-client",  NOMAD_CLIENT_IP, "nomad-client",  g, key, [ctx.commands.get("nomad-client-setup")!]);
}

// ── Legacy interface (kept for any direct callers) ─────────────────────────────
export interface HashistackArgs {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    debianCloudImageId: pulumi.Input<string>;
    cloudInitSnippetId: pulumi.Input<string>;
    cloudflaredTunnelToken: pulumi.Input<string>;
}

export interface HashistackResources {
    nomadSetup: command.local.Command;
    nomadClientSetup: command.local.Command;
    traefikSetup: command.local.Command;
}

export function createHashistack(
    { provider, infisicalConfig, sshKey, sshPrivateKey, debianCloudImageId, cloudInitSnippetId, cloudflaredTunnelToken }: HashistackArgs,
    commands?: Map<string, pulumi.Resource>,
): HashistackResources {
    const consulScriptPath = path.join(__dirname, "consul-setup.sh");
    const nomadScriptPath = path.join(__dirname, "nomad-setup.sh");
    const traefikScriptPath = path.join(__dirname, "traefik-setup.sh");
    const nomadClientScriptPath = path.join(__dirname, "nomad-client-setup.sh");
    const cloudflaredScriptPath = path.join(__dirname, "cloudflared-setup.sh");
    const helloWorldJobPath = path.join(__dirname, "hello-world.nomad.hcl");

    const consulContainer = new proxmox.ContainerLegacy("consul-server", {
        nodeName: "optiplex",
        vmId: CONSUL_VMID,
        cpu: { cores: 1 },
        memory: { dedicated: 256 },
        disk: { datastoreId: "local-lvm", size: 4 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "consul-server",
            ipConfigs: [{ ipv4: { address: `${CONSUL_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("consul-server", infisicalConfig), keys: [sshKey] },
        },
        tags: ["hashistack", "system"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const consulSetup = sshSetupUtil("consul-setup", CONSUL_IP, consulScriptPath, {
        CONSUL_IP,
        CONSUL_VERSION,
    }, sshPrivateKey, [consulContainer]);
    commands?.set("consul-setup", consulSetup);

    const consulProvider = new consul.Provider("consul", {
        address: `${CONSUL_IP}:8500`,
        scheme: "http",
    }, { dependsOn: [consulSetup] });

    const pulumi_services_node = new consul.Node("pulumi-services", {
        name: "pulumi-services",
        address: "127.0.0.1",
    }, { provider: consulProvider });
    commands?.set("consul-node", pulumi_services_node);

    new consul.Service("consul-ui", {
        name: "consul-ui",
        node: "pulumi-services",
        address: CONSUL_IP,
        port: 8500,
        tags: [
            "traefik.enable=true",
            `traefik.http.routers.consul-ui.rule=Host(\`consul.bagelindustries.com\`)`,
            "traefik.http.routers.consul-ui.entrypoints=web",
            "traefik.http.routers.consul-ui.middlewares=authentik@consulcatalog",
            "traefik.http.services.consul-ui.loadbalancer.server.port=8500",
        ],
    }, { provider: consulProvider, dependsOn: [pulumi_services_node] });
    commands?.set("consul-provider", consulProvider);

    const nomadContainer = new proxmox.ContainerLegacy("nomad-server", {
        nodeName: "optiplex",
        vmId: NOMAD_VMID,
        cpu: { cores: 2 },
        memory: { dedicated: 2048 },
        disk: { datastoreId: "local-lvm", size: 16 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "nomad-server",
            ipConfigs: [{ ipv4: { address: `${NOMAD_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("nomad-server", infisicalConfig), keys: [sshKey] },
        },
        tags: ["hashistack", "system"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const nomadSetup = sshSetupUtil("nomad-setup", NOMAD_IP, nomadScriptPath, {
        CONSUL_IP,
        CONSUL_VERSION,
        NOMAD_IP,
        NOMAD_VERSION,
    }, sshPrivateKey, [nomadContainer, consulContainer]);
    commands?.set("nomad-setup", nomadSetup);

    const traefikContainer = new proxmox.ContainerLegacy("traefik", {
        nodeName: "optiplex",
        vmId: TRAEFIK_VMID,
        cpu: { cores: 1 },
        memory: { dedicated: 256 },
        disk: { datastoreId: "local-lvm", size: 4 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "traefik",
            ipConfigs: [{ ipv4: { address: `${TRAEFIK_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("traefik", infisicalConfig), keys: [sshKey] },
        },
        tags: ["hashistack", "system"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const traefikSetup = sshSetupUtil("traefik-setup", TRAEFIK_IP, traefikScriptPath, {
        CONSUL_IP,
        TRAEFIK_VERSION,
    }, sshPrivateKey, [traefikContainer, nomadSetup]);
    commands?.set("traefik-setup", traefikSetup);

    const cloudflaredContainer = new proxmox.ContainerLegacy("cloudflared", {
        nodeName: "optiplex",
        vmId: CLOUDFLARED_VMID,
        cpu: { cores: 2 },
        memory: { dedicated: 1024 },
        disk: { datastoreId: "local-lvm", size: 2 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "cloudflared",
            ipConfigs: [{ ipv4: { address: `${CLOUDFLARED_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("cloudflared", infisicalConfig), keys: [sshKey] },
        },
        tags: ["system"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const cloudflaredSetup = new command.local.Command("cloudflared-setup", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
echo "Waiting for SSH on ${CLOUDFLARED_IP}..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${CLOUDFLARED_IP} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no root@${CLOUDFLARED_IP} "CLOUDFLARE_TUNNEL_TOKEN=$_TUNNEL_TOKEN bash -s" < "${cloudflaredScriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _SSH_KEY: sshPrivateKey,
            _TUNNEL_TOKEN: cloudflaredTunnelToken,
        },
    }, { dependsOn: [cloudflaredContainer, traefikSetup] });
    commands?.set("cloudflared-setup", cloudflaredSetup);

    const nomadClientVm = new proxmox.VmLegacy("nomad-client", {
        nodeName: "optiplex",
        vmId: NOMAD_CLIENT_VMID,
        name: "nomad-client",
        stopOnDestroy: true,
        agent: { enabled: true, trim: true, type: "virtio" },
        cpu: { cores: 4, type: "x86-64-v2-AES" },
        memory: { dedicated: 4096 },
        disks: [{
            datastoreId: "local-lvm",
            interface: "scsi0",
            importFrom: debianCloudImageId,
            size: 40,
            discard: "on",
            ssd: true,
        }],
        networkDevices: [{ bridge: "vmbr0", model: "virtio" }],
        initialization: {
            datastoreId: "local-lvm",
            ipConfigs: [{ ipv4: { address: `${NOMAD_CLIENT_IP}/24`, gateway: GATEWAY } }],
            userDataFileId: cloudInitSnippetId,
        },
        cdrom: { fileId: "none" },
        operatingSystem: { type: "l26" },
        tags: ["hashistack", "system"],
        onBoot: true,
        started: true,
    }, { provider, ignoreChanges: ["disks"] });

    const nomadClientSetup = sshSetupUtil("nomad-client-setup", NOMAD_CLIENT_IP, nomadClientScriptPath, {
        CONSUL_VERSION,
        CONSUL_IP,
        NOMAD_VERSION,
        NOMAD_CLIENT_IP,
    }, sshPrivateKey, [nomadClientVm, nomadSetup]);
    commands?.set("nomad-client-setup", nomadClientSetup);

    new command.local.Command("hello-world-deploy", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${NOMAD_IP} "nomad job run -" < "${helloWorldJobPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: { _SSH_KEY: sshPrivateKey },
    }, { dependsOn: [nomadClientSetup, traefikSetup] });

    return { nomadSetup, nomadClientSetup, traefikSetup };
}
