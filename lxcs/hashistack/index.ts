import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as consul from "@pulumi/consul";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

export const name = "hashistack";
export const provides = ["consul-setup", "nomad-setup", "traefik-setup"];
export const dependencies: string[] = [];

export const CONSUL_VERSION = "1.20.1";
export const NOMAD_VERSION = "1.9.4";
export const TRAEFIK_VERSION = "v3.3.1";

export const CONSUL_VMID = 201;
export const CONSUL_IP_CONST = ip(CONSUL_VMID);
export const NOMAD_VMID = 202;
export const NOMAD_IP = ip(NOMAD_VMID);
export const TRAEFIK_VMID = 203;
export const TRAEFIK_IP = ip(TRAEFIK_VMID);

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
            consul_version: CONSUL_VERSION
        },
        dependsOn: [consulMachine],
        // 201/202/203 were destroyed and rebuilt bare on update 167; bump to
        // reinstall consul/nomad/traefik on the new containers.
        generation: 1,
    });
    ctx.commands.set("consul-setup", consulProvision);

    const consulProvider = new consul.Provider("consul", {
        address: `${CONSUL_IP_CONST}:8500`,
        scheme: "http",
    }, { dependsOn: [consulProvision] });

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
            consul_version: CONSUL_VERSION,
            nomad_version: NOMAD_VERSION
        },
        dependsOn: [nomadMachine, consulProvision],
        generation: 1,
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
            consul_version: CONSUL_VERSION,
            traefik_version: TRAEFIK_VERSION
        },
        dependsOn: [traefikMachine, nomadProvision],
        generation: 1,
    });
    ctx.commands.set("traefik-setup", traefikProvision);
}
