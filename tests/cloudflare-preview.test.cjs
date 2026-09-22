const assert = require("node:assert/strict");
const { test } = require("node:test");
const pulumi = require("@pulumi/pulumi");
const cloudflare = require("@pulumi/cloudflare");
const { getMonitor } = require("@pulumi/pulumi/runtime/settings");

test("preview defers zone lookup when the new provider token is unknown", async () => {
    const requests = new Map();
    let lookups = 0;
    await pulumi.runtime.setMocks({
        newResource: args => ({ id: `${args.name}-id`, state: args.inputs }),
        call() {
            lookups++;
            // A provider configured with an unknown token can return an empty
            // result in preview, rather than an unknown account object.
            return {};
        },
    }, "pulumi-homelab", "preview", true);
    const monitor = getMonitor();
    const register = monitor.registerResource.bind(monitor);
    monitor.registerResource = (request, callback) => {
        requests.set(request.getName(), request.getObject().toJavaScript());
        return register(request, callback);
    };

    const provider = new cloudflare.Provider("preview-cloudflare", {
        apiToken: pulumi.secret(pulumi.unknown),
    });
    const { tunnelHostname } = require("../bin/framework/cloudflare-dns");
    const dns = tunnelHostname("preview-proxmox", {
        domain: "proxmox.raygen.dev",
        zone: "raygen.dev",
        tunnelToken: Buffer.from('{"t":"homelab-tunnel"}').toString("base64"),
        provider,
        accessEmails: ["raygenrrupe@gmail.com"],
    });
    await dns.id.promise();

    assert.equal(lookups, 0, "do not invoke Cloudflare before credentials are known");
    const unknown = "04da6b54-80e4-46f7-96ec-b56ff0331ba9";
    assert.equal(requests.get("preview-proxmox-access").accountId, unknown);
    assert.equal(requests.get("preview-proxmox-dns").zoneId, unknown);
});
