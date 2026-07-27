import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as consul from "@pulumi/consul";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

export const name = "hashistack";
export const provides = [
    "consul-setup",
    "nomad-setup",
    "traefik-setup",
    "nomad-client-setup",
    "cloudflared-setup",
];
export const dependencies: string[] = [];

export const CONSUL_VERSION = "1.20.1";
export const NOMAD_VERSION = "1.9.4";
export const TRAEFIK_VERSION = "v3.3.1";
export const CNI_VERSION = "1.5.1";

/** Nomad region/datacenter every homelab job targets (`datacenters = ["homelab"]`). */
export const HOMELAB_DATACENTER = "homelab";

export const CONSUL_VMID = 201;
export const CONSUL_IP_CONST = ip(CONSUL_VMID);
export const NOMAD_VMID = 202;
export const NOMAD_IP = ip(NOMAD_VMID);
export const TRAEFIK_VMID = 203;
export const TRAEFIK_IP = ip(TRAEFIK_VMID);
export const CLOUDFLARED_VMID = 204;
export const CLOUDFLARED_IP = ip(CLOUDFLARED_VMID);
export const NOMAD_CLIENT_VMID = 230;
export const NOMAD_CLIENT_IP = ip(NOMAD_CLIENT_VMID);

/** Host volume backing the lookout Postgres data directory. */
export const LOOKOUT_PGDATA_VOLUME = "lookout_pgdata";
export const LOOKOUT_PGDATA_PATH = `/opt/nomad/volumes/${LOOKOUT_PGDATA_VOLUME}`;

export function register(ctx: ServiceContext) {
    const consulMachine = new ProxmoxMachine("consul-server", {
        type: "lxc",
        vmId: CONSUL_VMID,
        ip: CONSUL_IP_CONST,
        cpu: 1,
        memory: 256,
        disk: 4,
        tags: ["hashistack", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword("consul-server", ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const consulProvision = ansibleProvision("consul-provision", {
        host: CONSUL_IP_CONST,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            role: "consul",
            services: ["consul"],
            datacenter: HOMELAB_DATACENTER,
            consul_version: CONSUL_VERSION
        },
        dependsOn: [consulMachine],
    });
    ctx.commands.set("consul-setup", consulProvision);

    const consulProvider = new consul.Provider("consul", {
        address: `${CONSUL_IP_CONST}:8500`,
        scheme: "http",
    }, { dependsOn: [consulProvision] });

    // Published so every other module registers its Traefik routes against a
    // Consul that is already up, instead of standing up a provider of its own.
    ctx.consulProvider = consulProvider;

    const nomadMachine = new ProxmoxMachine("nomad-server", {
        type: "lxc",
        vmId: NOMAD_VMID,
        ip: NOMAD_IP,
        cpu: 2,
        memory: 2048,
        disk: 16,
        tags: ["hashistack", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword("nomad-server", ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const nomadProvision = ansibleProvision("nomad-provision", {
        host: NOMAD_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            role: "nomad",
            consul_ip: CONSUL_IP_CONST,
            services: ["consul", "nomad"],
            datacenter: HOMELAB_DATACENTER,
            consul_version: CONSUL_VERSION,
            nomad_version: NOMAD_VERSION
        },
        dependsOn: [nomadMachine, consulProvision],
    });
    ctx.commands.set("nomad-setup", nomadProvision);

    const traefikMachine = new ProxmoxMachine("traefik", {
        type: "lxc",
        vmId: TRAEFIK_VMID,
        ip: TRAEFIK_IP,
        cpu: 1,
        memory: 256,
        disk: 4,
        tags: ["hashistack", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword("traefik", ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const traefikProvision = ansibleProvision("traefik-provision", {
        host: TRAEFIK_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            role: "traefik",
            consul_ip: CONSUL_IP_CONST,
            services: ["consul", "traefik"],
            datacenter: HOMELAB_DATACENTER,
            consul_version: CONSUL_VERSION,
            traefik_version: TRAEFIK_VERSION,
            // Only the tunnel (and the LAN) may set X-Forwarded-* headers.
            trusted_proxy_ips: [`${CLOUDFLARED_IP}/32`, "127.0.0.1/32"],
        },
        dependsOn: [traefikMachine, nomadProvision],
    });
    ctx.commands.set("traefik-setup", traefikProvision);

    // ── Nomad client ───────────────────────────────────────────────────────────
    // Every homelab job (demo, lookout, …) targets datacenter "homelab"; without
    // a client in that datacenter they stay queued forever. A VM rather than an
    // LXC because the jobs use the Docker driver with bridge networking.
    const nomadClientMachine = new ProxmoxMachine("nomad-client", {
        type: "vm",
        vmId: NOMAD_CLIENT_VMID,
        ip: NOMAD_CLIENT_IP,
        cpu: 4,
        memory: 6144,
        disk: 40,
        importFrom: ctx.debianCloudImageId,
        userDataFileId: ctx.cloudInitSnippetId,
        tags: ["hashistack", "system"],
        sshKeys: [ctx.sshKey],
        password: ctx.vmPassword,
    }, { provider: ctx.provider });

    const nomadClientProvision = ansibleProvision("nomad-client-provision", {
        host: NOMAD_CLIENT_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            role: "nomad-client",
            consul_ip: CONSUL_IP_CONST,
            nomad_ip: NOMAD_IP,
            services: ["consul", "nomad"],
            datacenter: HOMELAB_DATACENTER,
            consul_version: CONSUL_VERSION,
            nomad_version: NOMAD_VERSION,
            cni_version: CNI_VERSION,
            host_volumes: [
                { name: LOOKOUT_PGDATA_VOLUME, path: LOOKOUT_PGDATA_PATH },
            ],
        },
        dependsOn: [nomadClientMachine, nomadProvision],
    });
    ctx.commands.set("nomad-client-setup", nomadClientProvision);

    // ── Cloudflare tunnel ──────────────────────────────────────────────────────
    // The only path from the internet into the homelab: every Traefik route is
    // reachable externally through this tunnel and nothing else is.
    const cloudflaredMachine = new ProxmoxMachine("cloudflared", {
        type: "lxc",
        vmId: CLOUDFLARED_VMID,
        ip: CLOUDFLARED_IP,
        cpu: 1,
        memory: 256,
        disk: 4,
        tags: ["hashistack", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword("cloudflared", ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const cloudflaredProvision = ansibleProvision("cloudflared-provision", {
        host: CLOUDFLARED_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            role: "cloudflared",
            services: [],
            // Catch-all origin: anything the tunnel accepts is handed to Traefik,
            // which picks the backend from the Host header.
            traefik_origin: `http://${TRAEFIK_IP}:80`,
        },
        dependsOn: [cloudflaredMachine, traefikProvision],
    });

    // The tunnel token is a secret Output; ansibleProvision JSON-encodes its
    // extraVars and cannot carry one, so it is written over SSH instead. The
    // playbook installs the unit but leaves it stopped until this lands.
    const cloudflaredToken = new command.local.Command("cloudflared-token", {
        interpreter: ["/bin/bash", "-c"],
        create: `
set -euo pipefail
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
trap 'rm -f "$key"' EXIT
SSH="ssh -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10"
HOST=root@${CLOUDFLARED_IP}

# Piped over stdin so the token never appears in argv on either end.
printf 'TUNNEL_TOKEN=%s\n' "$_TUNNEL_TOKEN" | \\
  $SSH "$HOST" "install -d -m 0750 /etc/cloudflared && umask 077 && cat > /etc/cloudflared/cloudflared.env"

$SSH "$HOST" "systemctl enable --now cloudflared && systemctl restart cloudflared"
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _TUNNEL_TOKEN: pulumi.output(ctx.cloudflaredTunnelToken),
        },
        triggers: [pulumi.output(ctx.cloudflaredTunnelToken)],
    }, { dependsOn: [cloudflaredProvision] });

    ctx.commands.set("cloudflared-setup", cloudflaredToken);
}
