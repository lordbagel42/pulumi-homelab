const assert = require("node:assert/strict");
const { before, test } = require("node:test");
const pulumi = require("@pulumi/pulumi");
const cloudflare = require("@pulumi/cloudflare");
const { getMonitor } = require("@pulumi/pulumi/runtime/settings");

const resources = new Map();
const dependencies = new Map();
const tunnelToken = Buffer.from(JSON.stringify({
    a: "tunnel-account", t: "homelab-tunnel", s: "secret",
})).toString("base64");
let provider;

before(async () => {
    await pulumi.runtime.setMocks({
        newResource(args) {
            resources.set(args.name, args);
            return { id: `${args.name}-id`, state: args.inputs };
        },
        call(args) {
            assert.equal(args.token, "cloudflare:index/getZone:getZone");
            return {
                id: `${args.inputs.filter.name}-zone`,
                name: args.inputs.filter.name,
                account: { id: "zone-account", name: "Homelab" },
            };
        },
    }, "pulumi-homelab", "test");
    const monitor = getMonitor();
    const register = monitor.registerResource.bind(monitor);
    monitor.registerResource = (request, callback) => {
        dependencies.set(request.getName(), request.getDependenciesList());
        return register(request, callback);
    };
    provider = new cloudflare.Provider("test-cloudflare", { apiToken: "fake" });
});

test("protected DNS waits for an exact-host, email-only Access application", async () => {
    const { tunnelHostname } = require("../bin/framework/cloudflare-dns");
    const dns = tunnelHostname("gated", {
        domain: "admin.raygen.dev", zone: "raygen.dev", tunnelToken, provider,
        accessEmails: ["alice@example.com", "bob@another.example"],
    });
    await dns.id.promise();

    const app = resources.get("gated-access");
    assert.ok(app, "protected hostnames must create Access before DNS");
    assert.equal(app.type, "cloudflare:index/zeroTrustAccessApplication:ZeroTrustAccessApplication");
    assert.equal(app.inputs.accountId, "zone-account");
    assert.equal(app.inputs.type, "self_hosted");
    assert.equal(app.inputs.domain, "admin.raygen.dev");
    assert.deepEqual(app.inputs.destinations, [{ type: "public", uri: "admin.raygen.dev" }]);
    assert.deepEqual(app.inputs.policies, [{
        name: "Allowed emails", decision: "allow", precedence: 1,
        includes: [
            { email: { email: "alice@example.com" } },
            { email: { email: "bob@another.example" } },
        ],
    }]);
    assert.ok(dependencies.get("gated-dns").some(urn => urn.endsWith("::gated-access")));
    assert.equal(resources.get("gated-dns").inputs.zoneId, "raygen.dev-zone");
    assert.equal(resources.get("gated-dns").inputs.content, "homelab-tunnel.cfargotunnel.com");
});

test("existing ungated callers retain DNS-only behavior", async () => {
    const { tunnelHostname } = require("../bin/framework/cloudflare-dns");
    const dns = tunnelHostname("ungated", {
        domain: "homeassistant.bagelindustries.com", tunnelToken, provider,
    });
    await dns.id.promise();
    assert.equal(resources.has("ungated-access"), false);
    assert.equal(resources.get("ungated-dns").inputs.zoneId, "bagelindustries.com-zone");
    assert.equal(resources.get("ungated-dns").inputs.proxied, true);
});

test("an empty Access allowlist fails rather than publishing an unguarded hostname", () => {
    const { tunnelHostname } = require("../bin/framework/cloudflare-dns");
    assert.throws(() => tunnelHostname("empty", {
        domain: "empty.raygen.dev", zone: "raygen.dev", tunnelToken, provider,
        accessEmails: [],
    }), /Access.*email/i);
});

test("Proxmox uses the LAN HTTPS origin and waits for the gate and TLS transport", async () => {
    const consul = require("@pulumi/consul");
    const { register } = require("../bin/external/proxmox");
    pulumi.runtime.setAllConfig({
        "pulumi-homelab:PROXMOX_ENDPOINT": "https://192.168.0.7:8006/",
    });
    const traefik = new pulumi.CustomResource("test:index:Ready", "traefik-ready", {});
    const service = register({
        cloudflareProvider: provider,
        consulProvider: new consul.Provider("test-consul", { address: "localhost:8500" }),
        cloudflaredTunnelToken: tunnelToken,
        // CI uses this overlay address; the LAN proxy must not use it.
        proxmoxEndpoint: pulumi.secret("https://100.96.12.34:8006"),
        commands: new Map([["traefik-setup", traefik]]),
    });
    assert.ok(service, "register must create the Proxmox route");
    await service.id.promise();
    const { inputs } = resources.get("proxmox-service");
    assert.equal(await service.address.promise(), "192.168.0.7");
    assert.equal(await pulumi.isSecret(service.address), true);
    assert.equal(inputs.port, 8006);
    assert.equal(inputs.node, "proxmox-svc");
    const tags = Object.fromEntries(inputs.tags.map(tag => {
        const equals = tag.indexOf("=");
        return [tag.slice(0, equals), tag.slice(equals + 1)];
    }));
    assert.equal(tags["traefik.http.routers.proxmox.rule"], "Host(`proxmox.raygen.dev`)");
    assert.equal(tags["traefik.http.services.proxmox.loadbalancer.server.scheme"], "https");
    assert.equal(tags["traefik.http.services.proxmox.loadbalancer.serverstransport"], "proxmox@file");
    assert.equal(tags["traefik.http.routers.proxmox.middlewares"], "proxmox-tunnel-only@consulcatalog");
    assert.equal(tags["traefik.http.middlewares.proxmox-tunnel-only.ipallowlist.sourcerange"], "192.168.0.204/32");
    assert.equal(Object.keys(tags).some(key => key.includes("ipstrategy")), false);
    assert.deepEqual(resources.get("proxmox-access").inputs.policies[0].includes, [
        { email: { email: "raygenrrupe@gmail.com" } },
    ]);
    for (const name of ["proxmox-dns", "traefik-ready"]) {
        assert.ok(dependencies.get("proxmox-service").some(urn => urn.endsWith(`::${name}`)));
    }
});

test("Proxmox refuses to publish without a Cloudflare provider", () => {
    const { register } = require("../bin/external/proxmox");
    assert.throws(() => register({ commands: new Map() }), /Cloudflare/);
});
