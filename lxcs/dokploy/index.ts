import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../hashistack";

const DOKPLOY_VMID = 211;
const DOKPLOY_IP = ip(DOKPLOY_VMID);

export const name = "dokploy";
export const provides = ["dokploy-setup"];
export const dependencies: string[] = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: DOKPLOY_VMID,
        ip: DOKPLOY_IP,
        cpu: 4,
        memory: 10240,
        disk: 24,
        nesting: true,
        privileged: true,
        tags: ["dokploy", "app"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),
    }, { provider: ctx.provider });

    const provision = ansibleProvision("dokploy-provision", {
        host: DOKPLOY_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            consul_ip: CONSUL_IP_CONST,
            consul_version: CONSUL_VERSION,
        },
        dependsOn: [machine],
    });

    ctx.commands.set("dokploy-setup", provision);
}
