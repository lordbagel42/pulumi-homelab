import * as consul from "@pulumi/consul";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import type { ServiceContext } from "../../framework";
import { CLOUDFLARED_IP } from "../../lxcs/hashistack";

export const name = "june-console";
export const provides: string[] = [];
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): consul.Service {
    if (!ctx.cloudflareProvider) throw new Error("June console requires Cloudflare Access");
    if (!ctx.consulProvider) throw new Error("June console requires Consul");

    const domain = "june.raygen.dev";
    const dns = tunnelHostname(name, {
        domain,
        zone: "raygen.dev",
        tunnelToken: ctx.cloudflaredTunnelToken,
        provider: ctx.cloudflareProvider,
        accessEmails: ["raygenrrupe@gmail.com"],
    });
    // Routing only: no dependency on June's old provisioning archive or live
    // deployment controller. June's owner-session authentication stays enabled.
    const address = "192.168.0.215";
    const node = new consul.Node("june-console-node", {
        name: "june-console-svc",
        address,
    }, { provider: ctx.consulProvider });

    return new consul.Service("june-console-service", {
        name,
        node: node.name,
        address,
        port: 3080,
        tags: [
            "traefik.enable=true",
            // OAuth callbacks and one-time login credentials must not enter logs.
            "traefik.http.routers.june-console.observability.accesslogs=false",
            "traefik.http.routers.june-console-root.observability.accesslogs=false",
            "traefik.http.routers.june-console-deny.observability.accesslogs=false",
            `traefik.http.routers.june-console.rule=Host(\`${domain}\`) && (Path(\`/console\`) || PathPrefix(\`/console/\`) || (PathRegexp(\`^/[A-Za-z0-9_-]{24}$\`) && (Method(\`GET\`) || Method(\`HEAD\`))))`,
            "traefik.http.routers.june-console.priority=300",
            "traefik.http.routers.june-console.entrypoints=web",
            "traefik.http.routers.june-console.service=june-console",
            "traefik.http.routers.june-console.middlewares=june-console-tunnel-only@consulcatalog",
            "traefik.http.services.june-console.loadbalancer.server.port=3080",
            // No ipStrategy: use the socket peer, never X-Forwarded-For.
            `traefik.http.middlewares.june-console-tunnel-only.ipallowlist.sourcerange=${CLOUDFLARED_IP}/32`,
            `traefik.http.routers.june-console-root.rule=Host(\`${domain}\`) && Path(\`/\`)`,
            "traefik.http.routers.june-console-root.priority=300",
            "traefik.http.routers.june-console-root.entrypoints=web",
            "traefik.http.routers.june-console-root.service=noop@internal",
            "traefik.http.routers.june-console-root.middlewares=june-console-tunnel-only@consulcatalog,june-console-login@consulcatalog",
            "traefik.http.middlewares.june-console-login.redirectregex.regex=^https?://[^/]+/([?].*)?$",
            `traefik.http.middlewares.june-console-login.redirectregex.replacement=https://${domain}/console/session/login`,
            "traefik.http.middlewares.june-console-login.redirectregex.permanent=false",
            // Beat the global Authentik outpost router (priority 100). The
            // internal noop returns 418; it never contacts June or another app.
            `traefik.http.routers.june-console-deny.rule=Host(\`${domain}\`)`,
            "traefik.http.routers.june-console-deny.priority=200",
            "traefik.http.routers.june-console-deny.entrypoints=web",
            "traefik.http.routers.june-console-deny.service=noop@internal",
            "traefik.http.routers.june-console-deny.middlewares=june-console-tunnel-only@consulcatalog",
        ],
    }, { provider: ctx.consulProvider, dependsOn: [node, dns] });
}
