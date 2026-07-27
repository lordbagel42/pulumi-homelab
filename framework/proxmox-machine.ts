import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as consul from "@pulumi/consul";

export interface ProxmoxMachineArgs {
    type: "lxc" | "vm";
    nodeName?: string;
    vmId: number;
    cpu?: number;
    memory?: number;
    disk?: number;
    datastoreId?: string;
    templateFileId?: string;
    importFrom?: pulumi.Input<string>;
    ip: string;
    gateway?: string;
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

        if (args.type === "lxc") {
            this.machine = new proxmox.ContainerLegacy(name, {
                nodeName: args.nodeName || "optiplex",
                vmId: args.vmId,
                cpu: { cores: args.cpu || 1 },
                memory: { dedicated: args.memory || 512 },
                disk: { datastoreId: args.datastoreId || "local-lvm", size: args.disk || 8 },
                operatingSystem: {
                    templateFileId: args.templateFileId || "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
                    type: "debian",
                },
                networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
                initialization: {
                    hostname: name,
                    ipConfigs: [{ ipv4: { address: `${args.ip}/24`, gateway } }],
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
                    ipConfigs: [{ ipv4: { address: `${args.ip}/24`, gateway } }],
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

        // Register a node for the machine if it doesn't exist
        const node = new consul.Node(`${name}-node`, {
            name: name,
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
