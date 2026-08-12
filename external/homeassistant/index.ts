import * as consul from "@pulumi/consul";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import type { ServiceContext } from "../../framework";

// Home Assistant runs on a box this stack does not create — it is its own
// appliance-style install, managed outside Pulumi. All that lives here is the
// catalog entry that puts it behind the homelab Traefik, so it gets the same
// route + tunnel treatment as everything else without pretending Pulumi owns
// the machine.
//
// Because it is not a Proxmox guest, it does not follow the vmId = last octet
// rule: .200 is the top of the Eero's DHCP pool and needs a reservation there
// (or a static address on the host) or this route points at whatever answers
// next.

export const name = "homeassistant";
export const provides: string[] = [];
export const dependencies: string[] = ["consul-setup"];

export const HOMEASSISTANT_IP = "192.168.0.200";
export const HOMEASSISTANT_PORT = 8123;
export const HOMEASSISTANT_DOMAIN = "homeassistant.bagelindustries.com";

export function register(ctx: ServiceContext): void {
    // DNS: a proxied CNAME onto the homelab tunnel. The tunnel's ingress is a
    // catch-all to Traefik, so this record is the entire externally-facing half
    // of the route.
    if (ctx.cloudflareProvider) {
        tunnelHostname(name, {
            domain: HOMEASSISTANT_DOMAIN,
            tunnelToken: ctx.cloudflaredTunnelToken,
            provider: ctx.cloudflareProvider,
        });
    }

    if (!ctx.consulProvider) return;

    // Synthetic node name, same reason as every other catalog registration
    // here: a node that a live Consul agent owns has its service list
    // reconciled by anti-entropy, which deletes anything the agent itself does
    // not know about. Home Assistant runs no agent at all, but the name is kept
    // in the `-svc` shape so that stays true if one is ever added.
    const node = new consul.Node("homeassistant-node", {
        name: "homeassistant-svc",
        address: HOMEASSISTANT_IP,
    }, { provider: ctx.consulProvider });

    // Deliberately NOT `protected`. Home Assistant does its own auth, and the
    // companion apps and every webhook-style integration talk to it with
    // long-lived tokens — a forwardauth in front would bounce all of them to a
    // login page they cannot complete.
    new consul.Service("homeassistant-service", {
        name: name,
        node: node.name,
        address: HOMEASSISTANT_IP,
        port: HOMEASSISTANT_PORT,
        tags: [
            "traefik.enable=true",
            `traefik.http.routers.${name}.rule=Host(\`${HOMEASSISTANT_DOMAIN}\`)`,
            `traefik.http.routers.${name}.entrypoints=web`,
            `traefik.http.services.${name}.loadbalancer.server.port=${HOMEASSISTANT_PORT}`,
        ],
    }, { provider: ctx.consulProvider, dependsOn: [node] });
}
