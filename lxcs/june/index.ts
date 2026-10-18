import * as consul from "@pulumi/consul";
import * as pulumi from "@pulumi/pulumi";
import { ServiceContext } from "../../framework";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { readSecret } from "../../infisical";

export const name = "june";
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    if (!ctx.consulProvider || !ctx.cloudflareProvider) {
        throw new Error("June ingress requires Consul and Cloudflare");
    }
    // The application deployment controller owns releases, credentials and
    // systemd. Do not recreate the retired june-stage/source/provision commands.
    const machine = new ProxmoxMachine(
        name,
        {
            type: "lxc",
            nodeName: "optiplex",
            vmId: 215,
            ip: "192.168.0.215",
            cpu: 2,
            memory: 4096,
            disk: 96,
            nesting: true,
            tags: ["ai", "june"],
            sshKeys: [ctx.sshKey],
            password: readSecret("JUNE_ROOT_PASSWORD", {
                ...ctx.infisicalConfig,
                secretPath: "/june",
            }),
        },
        { provider: ctx.provider, protect: true },
    );
    const domain = "june-slack.bagelindustries.com";
    tunnelHostname("june-slack", {
        domain,
        tunnelToken: ctx.cloudflaredTunnelToken,
        provider: ctx.cloudflareProvider,
    });
    const node = new consul.Node(
        "june-slack-node",
        {
            name: "june-slack-svc",
            address: machine.ip,
        },
        { provider: ctx.consulProvider },
    );
    new consul.Service(
        "june-slack-service",
        {
            name: "june-slack",
            node: node.name,
            address: machine.ip,
            // The deployment responder owns Slack ingress, independently of June restarts.
            port: 3083,
            tags: [
                "traefik.enable=true",
                `traefik.http.routers.june-slack.rule=Host(\`${domain}\`) && Path(\`/webhooks/slack\`) && Method(\`POST\`)`,
                "traefik.http.routers.june-slack.entrypoints=web",
                "traefik.http.routers.june-slack.service=june-slack",
                "traefik.http.services.june-slack.loadbalancer.server.port=3083",
            ],
        },
        { provider: ctx.consulProvider, dependsOn: [node, machine.machine] },
    );
}
