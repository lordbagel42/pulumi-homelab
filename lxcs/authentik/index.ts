import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as consul from "@pulumi/consul";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword, managedSecret, readSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";
import { CONSUL_IP_CONST } from "../hashistack";

export const name = "authentik";
export const provides = ["authentik-setup"];
export const dependencies: string[] = ["consul-setup"];

export function register(ctx: ServiceContext) {
    const VMID = 220;
    const IP = ip(VMID);

    const authentik = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: VMID,
        ip: IP,
        cpu: 2,
        memory: 4096,
        disk: 20,
        nesting: true,
        privileged: true,
        tags: ["auth", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),
        reverseProxy: {
            domain: "auth.bagelindustries.com",
            port: 9000,
            entrypoint: "web",
        },
        consulProvider: new consul.Provider("authentik-consul", {
            address: `${CONSUL_IP_CONST}:8500`,
            scheme: "http",
        }),
    }, { provider: ctx.provider });

    const provision = ansibleProvision("authentik-provision", {
        host: IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            consul_ip: CONSUL_IP_CONST,
            pg_pass: managedSecret("authentik-pg-pass", ctx.infisicalConfig),
            secret_key: managedSecret("authentik-secret-key", ctx.infisicalConfig),
            bootstrap_password: readSecret("ADMIN_USER_PASS", { ...ctx.infisicalConfig, secretPath: "/" }),
            bootstrap_token: managedSecret("authentik-bootstrap-token", ctx.infisicalConfig),
            bootstrap_email: managedSecret("authentik-bootstrap-email", ctx.infisicalConfig),
        },
        dependsOn: [authentik],
    });

    ctx.commands.set("authentik-setup", provision);
}
