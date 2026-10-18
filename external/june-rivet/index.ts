import * as fs from "node:fs";
import * as path from "node:path";
import * as pulumi from "@pulumi/pulumi";
import * as cloudflare from "@pulumi/cloudflare";
import * as command from "@pulumi/command";
import type { ServiceContext } from "../../framework";

export const name = "june-rivet";
export const provides: string[] = [];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): cloudflare.DnsRecord {
    if (!ctx.cloudflareProvider) throw new Error("June Rivet requires Cloudflare Access");
    const provider = ctx.cloudflareProvider;
    const domain = "june-rivet.raygen.dev";
    const zone = cloudflare.getZoneOutput(
        {
            filter: { name: pulumi.unsecret(provider.apiToken.apply(() => "raygen.dev")) },
        },
        { provider },
    );
    const accountId = zone.account.apply((account) => account.id);
    const access = new cloudflare.ZeroTrustAccessApplication(
        "june-rivet-access",
        {
            accountId,
            name: domain,
            type: "self_hosted",
            domain,
            destinations: [{ type: "public", uri: domain }],
            sessionDuration: "8h",
            httpOnlyCookieAttribute: true,
            optionsPreflightBypass: false,
            // Same identity restriction as the live June console, for every path.
            policies: [
                {
                    name: "Allowed emails",
                    decision: "allow",
                    precedence: 1,
                    includes: [{ email: { email: "raygenrrupe@gmail.com" } }],
                },
            ],
        },
        { provider },
    );
    // A separate connector can reach Rivet's loopback listener without opening
    // a LAN port or changing the shared tunnel, Traefik, or June's application.
    const tunnel = new cloudflare.ZeroTrustTunnelCloudflared(
        "june-rivet-tunnel",
        {
            accountId,
            name,
            configSrc: "cloudflare",
        },
        { provider },
    );
    const tunnelConfig = new cloudflare.ZeroTrustTunnelCloudflaredConfig(
        "june-rivet-tunnel-config",
        {
            accountId,
            tunnelId: tunnel.id,
            config: {
                ingresses: [
                    {
                        hostname: domain,
                        service: "http://127.0.0.1:6420",
                        originRequest: {
                            access: { required: true, teamName: "bagel", audTags: [access.aud] },
                        },
                    },
                    { service: "http_status:404" },
                ],
            },
        },
        { provider },
    );
    const token = pulumi.secret(
        cloudflare.getZeroTrustTunnelCloudflaredTokenOutput(
            {
                accountId,
                tunnelId: tunnel.id,
            },
            { provider },
        ).token,
    );
    const directory = path.resolve("external/june-rivet");
    const installer = fs.readFileSync(path.join(directory, "install.py"), "utf8");
    const service = fs.readFileSync(path.join(directory, "june-rivet-tunnel.service"), "utf8");
    const payload = token.apply((value) => JSON.stringify({ token: value, service }));
    const install = `python3 -c '${installer.replace(/'/g, `'"'"'`)}'`;
    const connector = new command.remote.Command(
        "june-rivet-connector",
        {
            connection: {
                host: "192.168.0.215",
                user: "root",
                privateKey: ctx.sshPrivateKey,
                // Existing console-verified june:sshHostKey; never trust a keyscan.
                hostKey:
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKMleDIvdMlNClOu0GIe+kX5XMHMKCcXIHMO57oX8ctR",
                dialErrorLimit: 2,
                perDialTimeout: 10,
            },
            create: install,
            update: install,
            stdin: payload,
            triggers: [installer, payload],
            logging: "none",
            addPreviousOutputInEnv: false,
        },
        {
            dependsOn: [tunnelConfig],
            additionalSecretOutputs: ["stdout", "stderr"],
            customTimeouts: { create: "5m", update: "5m" },
        },
    );
    return new cloudflare.DnsRecord(
        "june-rivet-dns",
        {
            zoneId: zone.id,
            name: domain,
            type: "CNAME",
            content: pulumi.interpolate`${tunnel.id}.cfargotunnel.com`,
            proxied: true,
            ttl: 1,
            comment: "managed by pulumi-homelab",
        },
        { provider, dependsOn: [access, connector] },
    );
}
