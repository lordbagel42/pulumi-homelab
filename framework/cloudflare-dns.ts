import * as pulumi from "@pulumi/pulumi";
import * as cloudflare from "@pulumi/cloudflare";

/** The zone every homelab hostname lives in. */
export const ZONE = "bagelindustries.com";

/**
 * Tunnel UUID out of a cloudflared connector token.
 *
 * The token is base64-encoded JSON — `{"a": account tag, "t": tunnel id,
 * "s": tunnel secret}` — which means the stack already knows which tunnel it is
 * pointing DNS at and needs no separate tunnel-id config value to keep in sync.
 *
 * Only `t` leaves this function. The tunnel secret stays inside the apply, and
 * the failure messages below deliberately quote nothing from the decoded body.
 * The UUID itself is not a credential (it is the public half — it appears in
 * every `*.cfargotunnel.com` CNAME), so the result is unsecreted to keep
 * `pulumi preview` diffs readable rather than a wall of `[secret]`.
 */
export function tunnelId(token: pulumi.Input<string>): pulumi.Output<string> {
    return pulumi.unsecret(pulumi.output(token).apply((raw) => {
        let decoded: { t?: unknown };
        try {
            decoded = JSON.parse(Buffer.from(raw.trim(), "base64").toString("utf-8"));
        } catch {
            throw new Error(
                "cloudflared tunnel token does not base64-decode to JSON — " +
                "it is probably an API token or a tunnel *name*, not a connector token",
            );
        }
        if (typeof decoded.t !== "string" || decoded.t === "") {
            throw new Error('cloudflared tunnel token carries no tunnel id ("t")');
        }
        return decoded.t;
    }));
}

// One zone lookup per zone name, shared by every hostname. Without the cache
// each call would be its own invoke against the Cloudflare API on every preview.
const zones = new Map<string, pulumi.Output<cloudflare.GetZoneResult>>();

function getZone(zone: string, provider: cloudflare.Provider): pulumi.Output<cloudflare.GetZoneResult> {
    let result = zones.get(zone);
    if (!result) {
        // Invokes do not defer for unknown provider credentials alone. Make
        // the lookup input unknown too until the token exists, so a first
        // preview cannot treat an empty provider result as a resolved zone.
        // Only the public zone name leaves this apply, never the API token.
        const name = pulumi.unsecret(provider.apiToken.apply(() => zone));
        result = cloudflare.getZoneOutput({ filter: { name } }, { provider });
        zones.set(zone, result);
    }
    return result;
}

export interface TunnelHostnameArgs {
    /** FQDN to route through the tunnel, e.g. `homeassistant.bagelindustries.com`. */
    domain: string;
    /** Connector token of the tunnel that should answer for `domain`. */
    tunnelToken: pulumi.Input<string>;
    provider: cloudflare.Provider;
    /** Defaults to ZONE; pass this only for a hostname in another zone. */
    zone?: string;
    /** When set, require Cloudflare Access and allow only these exact emails. */
    accessEmails?: string[];
}

/**
 * Points a hostname at a Cloudflare tunnel: one proxied CNAME to
 * `<tunnel-id>.cfargotunnel.com`, which is the whole of what "add a public
 * hostname" does in the dashboard *given that the tunnel's ingress already has
 * a catch-all rule*. Both tunnels here do — they hand everything to a Traefik
 * that picks the backend off the Host header — so a new route is DNS and
 * nothing else. See docs/external-routing.md.
 */
export function tunnelHostname(name: string, args: TunnelHostnameArgs): cloudflare.DnsRecord {
    if (args.accessEmails !== undefined && args.accessEmails.length === 0) {
        throw new Error("Cloudflare Access requires at least one allowed email");
    }
    const zone = getZone(args.zone ?? ZONE, args.provider);
    const access = args.accessEmails === undefined ? undefined : new cloudflare.ZeroTrustAccessApplication(`${name}-access`, {
        accountId: zone.account.apply(account => account.id),
        name: args.domain,
        type: "self_hosted",
        domain: args.domain,
        destinations: [{ type: "public", uri: args.domain }],
        sessionDuration: "8h",
        httpOnlyCookieAttribute: true,
        // Use the account's existing login methods. Authentication alone is
        // not authorization: no Everyone, domain-wide, or bypass policy.
        policies: [{
            name: "Allowed emails",
            decision: "allow",
            precedence: 1,
            includes: args.accessEmails.map(email => ({ email: { email } })),
        }],
    }, { provider: args.provider });

    return new cloudflare.DnsRecord(`${name}-dns`, {
        zoneId: zone.id,
        name: args.domain,
        type: "CNAME",
        content: pulumi.interpolate`${tunnelId(args.tunnelToken)}.cfargotunnel.com`,
        // Proxied, so the tunnel is reachable at all: an unproxied
        // `*.cfargotunnel.com` record resolves to nothing.
        proxied: true,
        // 1 = automatic, which is the only TTL a proxied record accepts.
        ttl: 1,
        comment: "managed by pulumi-homelab",
    }, { provider: args.provider, dependsOn: access ? [access] : [] });
}
