import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as consul from "@pulumi/consul";
import { LXC_TEMPLATE_FILE } from "./node-assets";

/**
 * Prefix length for every machine's primary address.
 *
 * /16, not /24, and this is load-bearing rather than cosmetic — it is what makes
 * the homelab reachable from the mrow Kubernetes cluster.
 *
 * The LAN is ONE flat layer-2 broadcast domain with a consumer Eero that cannot
 * be given static routes. Three different /24s live on it:
 *
 *   192.168.0.0/24    this homelab. Eero DHCP pool + the static range below.
 *   192.168.10.0/24   mrow's Talos nodes, statically addressed with a /16.
 *   192.168.11-12.x   mrow's Cilium LB-IPAM service addresses, ARP-announced.
 *
 * With a /24 here, a packet arriving from 192.168.10.11 looks off-subnet, so the
 * reply is handed to the default gateway — and the Eero has no route for
 * 192.168.10.0/24, so it treats the reply as internet-bound and drops it. The
 * result is a one-way path: mrow's ARP for 192.168.0.201 succeeds (same L2), the
 * request arrives, and the response never comes back.
 *
 * That is exactly the symptom recorded in talos-things' design doc — the cluster
 * nodes could not reach 192.168.0.201 on any port, while a laptop at
 * 192.168.0.71 (inside the /24, so replies are on-link) reached it fine. It was
 * logged as "most likely a /24 netmask or a firewall"; it was the netmask, and
 * it was here.
 *
 * A /16 makes 192.168.10.x and the LB-IPAM ranges on-link, so replies go out by
 * ARP over the shared segment instead of via a router that knows nothing about
 * them. It also matches what mrow's nodes already do (`address: 192.168.10.11/16`
 * in talos-things' values.yaml), which is what makes the path symmetric.
 *
 * Widening is safe: every address previously on-link still is. The catch is that
 * it does not apply to a running machine on its own — see the note in
 * proxmox-machine.ts.
 */
export const LAN_PREFIX = 16;

export interface ProxmoxMachineArgs {
    type: "lxc" | "vm";
    nodeName?: string;
    vmId: number;
    cpu?: number;
    memory?: number;
    disk?: number;
    datastoreId?: string;
    templateFileId?: pulumi.Input<string>;
    importFrom?: pulumi.Input<string>;
    ip: string;
    gateway?: string;
    /**
     * Prefix length for the primary address. Defaults to LAN_PREFIX (/16), which
     * is what lets machines here reply to mrow's 192.168.10.x nodes over the
     * shared flat L2 — see the comment on LAN_PREFIX above. Override only for a
     * machine that must be confined to its own /24.
     */
    cidrPrefix?: number;
    tags?: string[];
    nesting?: boolean;
    privileged?: boolean;

    // Auth
    sshKeys: string[];
    password: pulumi.Input<string>;
    userDataFileId?: pulumi.Input<string>;

    // Optional reverse proxy / consul registration
    reverseProxy?: {
        domain: string;
        port: number;
        protected?: boolean;
        entrypoint?: string;
        /** Extra Consul tags, e.g. Traefik middleware definitions owned by this service. */
        extraTags?: string[];
    };
    consulProvider?: consul.Provider;
}

export class ProxmoxMachine extends pulumi.ComponentResource {
    public readonly ip: string;
    public readonly machine: proxmox.ContainerLegacy | proxmox.VmLegacy;

    constructor(name: string, args: ProxmoxMachineArgs, opts?: pulumi.ComponentResourceOptions) {
        super("homelab:framework:ProxmoxMachine", name, {}, opts);

        this.ip = args.ip;
        const gateway = args.gateway || "192.168.0.1";
        // /16 by default. A /24 here breaks the return path to mrow's
        // 192.168.10.x nodes on this same flat L2 — the full reasoning is on
        // LAN_PREFIX above, and it is the bug that kept Consul cluster
        // peering from ever establishing.
        //
        // NOTE this does not reconfigure a machine that is already running.
        // Proxmox writes the address into the guest at creation (into
        // /etc/network/interfaces for an LXC, via cloud-init for a VM), so an
        // existing guest keeps its old /24 until it is restarted and re-reads
        // that config. After `pulumi up`, restart the affected guests — or at
        // minimum consul-server (201) — and confirm with `ip -4 addr show`.
        const prefix = args.cidrPrefix ?? LAN_PREFIX;

        if (args.type === "lxc") {
            this.machine = new proxmox.ContainerLegacy(name, {
                nodeName: args.nodeName || "optiplex",
                vmId: args.vmId,
                cpu: { cores: args.cpu || 1 },
                memory: { dedicated: args.memory || 512 },
                disk: { datastoreId: args.datastoreId || "local-lvm", size: args.disk || 8 },
                operatingSystem: {
                    templateFileId: args.templateFileId ?? `local:vztmpl/${LXC_TEMPLATE_FILE}`,
                    type: "debian",
                },
                networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
                initialization: {
                    hostname: name,
                    ipConfigs: [{ ipv4: { address: `${args.ip}/${prefix}`, gateway } }],
                    userAccount: {
                        password: args.password,
                        keys: args.sshKeys,
                    },
                },
                features: { nesting: args.nesting || false },
                unprivileged: !args.privileged,
                tags: args.tags || [],
                startOnBoot: true,
                started: true,
            }, { parent: this, ...opts });
        } else {
            this.machine = new proxmox.VmLegacy(name, {
                nodeName: args.nodeName || "optiplex",
                vmId: args.vmId,
                name: name,
                agent: { enabled: true, trim: true, type: "virtio" },
                cpu: { cores: args.cpu || 1, type: "x86-64-v2-AES" },
                memory: { dedicated: args.memory || 1024 },
                disks: [{
                    datastoreId: args.datastoreId || "local-lvm",
                    interface: "scsi0",
                    importFrom: args.importFrom,
                    size: args.disk || 20,
                    discard: "on",
                    ssd: true,
                }],
                networkDevices: [{ bridge: "vmbr0", model: "virtio" }],
                initialization: {
                    datastoreId: args.datastoreId || "local-lvm",
                    ipConfigs: [{ ipv4: { address: `${args.ip}/${prefix}`, gateway } }],
                    userDataFileId: args.userDataFileId,
                },
                cdrom: { fileId: "none" },
                operatingSystem: { type: "l26" },
                tags: args.tags || [],
                onBoot: true,
                started: true,
            }, { parent: this, ...opts, ignoreChanges: ["disks"] });
        }

        if (args.reverseProxy && args.consulProvider) {
            this.registerConsulService(name, args.reverseProxy, args.consulProvider);
        }

        this.registerOutputs({
            ip: this.ip,
        });
    }

    private registerConsulService(name: string, rp: NonNullable<ProxmoxMachineArgs["reverseProxy"]>, provider: consul.Provider) {
        const tags = [
            "traefik.enable=true",
            `traefik.http.routers.${name}.rule=Host(\`${rp.domain}\`)`,
            `traefik.http.routers.${name}.entrypoints=${rp.entrypoint || "web"}`,
            `traefik.http.services.${name}.loadbalancer.server.port=${rp.port}`,
            ...(rp.protected ? [`traefik.http.routers.${name}.middlewares=authentik@consulcatalog`] : []),
            ...(rp.extraTags ?? []),
        ];

        // The node name must NOT match the machine's own Consul agent node name
        // (which defaults to the hostname, i.e. `name`). A catalog service
        // registered against a node that has a live agent is wiped by Consul's
        // anti-entropy sync: the agent reconciles its own — empty — service list
        // and deregisters everything else on that node. That is what kept
        // silently removing the authentik service and 404-ing its route, while
        // the consul-ui/nomad-ui registrations survived because their node names
        // belong to no agent. Use a synthetic node name for the same reason.
        const node = new consul.Node(`${name}-node`, {
            name: `${name}-svc`,
            address: this.ip,
        }, { provider, parent: this });

        new consul.Service(`${name}-service`, {
            name: name,
            node: node.name,
            address: this.ip,
            port: rp.port,
            tags: tags,
        }, { provider, parent: this, dependsOn: [node] });
    }
}
