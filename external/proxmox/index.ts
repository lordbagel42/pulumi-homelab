import * as pulumi from "@pulumi/pulumi";
import * as consul from "@pulumi/consul";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import type { ServiceContext } from "../../framework";
import { CLOUDFLARED_IP } from "../../lxcs/hashistack";

export const name = "proxmox";
export const provides: string[] = [];
export const dependencies = ["consul-setup", "traefik-setup"];

export function register(ctx: ServiceContext): consul.Service {
    // Unlike optional public routes, this admin route must never fail open.
    if (!ctx.cloudflareProvider) throw new Error("Proxmox requires Cloudflare Access");
    if (!ctx.consulProvider) throw new Error("Proxmox requires Consul");
    const traefik = ctx.commands.get("traefik-setup");
    if (!traefik) throw new Error("Proxmox requires the Traefik TLS transport");

    // ctx.proxmoxEndpoint can be the CI-only NetBird endpoint. Traefik lives
    // on the LAN, so always use the LAN endpoint for the optiplex origin.
    const address = new pulumi.Config().requireSecret("PROXMOX_ENDPOINT")
        .apply(endpoint => new URL(endpoint).hostname);
    const domain = "proxmox.raygen.dev";
    const dns = tunnelHostname(name, {
        domain,
        zone: "raygen.dev",
        tunnelToken: ctx.cloudflaredTunnelToken,
        provider: ctx.cloudflareProvider,
        accessEmails: ["raygenrrupe@gmail.com"],
    });

    const node = new consul.Node("proxmox-node", {
        name: "proxmox-svc",
        address,
    }, { provider: ctx.consulProvider });

    return new consul.Service("proxmox-service", {
        name,
        node: node.name,
        address,
        port: 8006,
        tags: [
            "traefik.enable=true",
            `traefik.http.routers.${name}.rule=Host(\`${domain}\`)`,
            `traefik.http.routers.${name}.entrypoints=web`,
            `traefik.http.services.${name}.loadbalancer.server.port=8006`,
            `traefik.http.services.${name}.loadbalancer.server.scheme=https`,
            // Only this backend accepts Proxmox's self-signed certificate.
            `traefik.http.services.${name}.loadbalancer.serverstransport=proxmox@file`,
            `traefik.http.routers.${name}.middlewares=proxmox-tunnel-only@consulcatalog`,
            // Match the socket peer, NOT X-Forwarded-For, to prevent a direct
            // request to Traefik from bypassing the Cloudflare Access gate.
            `traefik.http.middlewares.proxmox-tunnel-only.ipallowlist.sourcerange=${CLOUDFLARED_IP}/32`,
        ],
    }, { provider: ctx.consulProvider, dependsOn: [node, dns, traefik] });
}
