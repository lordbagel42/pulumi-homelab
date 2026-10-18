import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as consul from "@pulumi/consul";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import type { ServiceContext } from "../../framework";

export const name = "codex-api";
export const provides = ["codex-api-host"];
export const dependencies: string[] = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    const provider = new proxmox.Provider("codex-api-proxmox", {
        endpoint: ctx.proxmoxEndpoint,
        username: ctx.proxmoxUsername,
        password: ctx.proxmoxPassword,
        insecure: new pulumi.Config().getBoolean("PROXMOX_INSECURE") ?? false,
        ssh: {
            username: "root",
            password: ctx.proxmoxPassword,
            nodes: [{ name: "optiplex", address: "192.168.0.10" }],
        },
    });
    const image = new proxmox.download.File(
        "codex-api-image",
        {
            contentType: "import",
            datastoreId: "local",
            nodeName: "optiplex",
            url: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
            fileName: "codex-api-debian-12.qcow2",
            overwrite: false,
            overwriteUnmanaged: false,
        },
        { provider },
    );
    const cloudInit = new proxmox.FileLegacy(
        "codex-api-cloud-init",
        {
            contentType: "snippets",
            datastoreId: "local",
            nodeName: "optiplex",
            sourceRaw: {
                fileName: "codex-api-cloud-init.yaml",
                data: `#cloud-config
hostname: codex-api
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: true
    ssh_authorized_keys:
      - ${ctx.sshKey}
  - name: codex
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - ${ctx.sshKey}
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
ssh_pwauth: false
`,
            },
        },
        { provider, deleteBeforeReplace: true },
    );
    const machine = new ProxmoxMachine(
        name,
        {
            type: "vm",
            nodeName: "optiplex",
            vmId: 234,
            ip: "192.168.0.234",
            cpu: 3,
            memory: 3072,
            disk: 48,
            datastoreId: "hdd",
            importFrom: image.id,
            userDataFileId: cloudInit.id,
            sshKeys: [ctx.sshKey],
            password: ctx.vmPassword,
            tags: ["codex", "private-api"],
        },
        { provider, protect: true },
    );
    ctx.commands.set("codex-api-host", machine);
    // The gateway key is provisioned directly in Infisical, never command stdout.
    if (config.getBoolean("publish") && ctx.consulProvider) {
        const node = new consul.Node(
            "codex-api-node",
            {
                name: "codex-api-svc",
                address: "192.168.0.234",
            },
            { provider: ctx.consulProvider },
        );
        new consul.Service(
            "codex-api-service",
            {
                name,
                node: node.name,
                address: "192.168.0.234",
                port: 8787,
                tags: [
                    "traefik.enable=true",
                    "traefik.http.routers.codex-api.rule=Host(`codex-api.bagelindustries.com`)",
                    "traefik.http.routers.codex-api.entrypoints=web",
                    "traefik.http.services.codex-api.loadbalancer.server.port=8787",
                ],
            },
            { provider: ctx.consulProvider },
        );
    }
}
