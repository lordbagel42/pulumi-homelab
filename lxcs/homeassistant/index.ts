import * as consul from "@pulumi/consul";
import type { ServiceContext } from "../../framework";
import { CONSUL_IP_CONST } from "../hashistack";

// Home Assistant already runs outside this stack (existing host, not a
// Pulumi-managed LXC/VM) — this module only registers it with Consul so
// Traefik's consulcatalog provider picks up the route.
export const name = "homeassistant";
export const provides = ["homeassistant-setup"];
export const dependencies: string[] = ["consul-setup"];

const HOMEASSISTANT_IP = "192.168.0.200";
const HOMEASSISTANT_PORT = 8123;
const HOMEASSISTANT_DOMAIN = "homeassistant.bagelindustries.com";

export function register(ctx: ServiceContext) {
    const consulProvider = new consul.Provider("homeassistant-consul", {
        address: `${CONSUL_IP_CONST}:8500`,
        scheme: "http",
    });

    const node = new consul.Node("homeassistant-node", {
        name: "homeassistant",
        address: HOMEASSISTANT_IP,
    }, { provider: consulProvider });

    const service = new consul.Service("homeassistant-service", {
        name: "homeassistant",
        node: node.name,
        address: HOMEASSISTANT_IP,
        port: HOMEASSISTANT_PORT,
        tags: [
            "traefik.enable=true",
            `traefik.http.routers.homeassistant.rule=Host(\`${HOMEASSISTANT_DOMAIN}\`)`,
            "traefik.http.routers.homeassistant.entrypoints=web",
            `traefik.http.services.homeassistant.loadbalancer.server.port=${HOMEASSISTANT_PORT}`,
        ],
    }, { provider: consulProvider, dependsOn: [node] });

    ctx.commands.set("homeassistant-setup", service);
}
