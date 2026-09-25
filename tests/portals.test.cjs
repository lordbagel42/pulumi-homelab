const assert = require("node:assert/strict");
const { test } = require("node:test");
const pulumi = require("@pulumi/pulumi");
const cloudflare = require("@pulumi/cloudflare");
const consul = require("@pulumi/consul");
const { getMonitor } = require("@pulumi/pulumi/runtime/settings");

test("portals deploy with exact-owner Access, verified audience and restricted routing, not privileged agent credentials", async () => {
  const resources = new Map();
  const dependencies = new Map();
  await pulumi.runtime.setMocks(
    {
      newResource(args) {
        resources.set(args.name, args);
        return {
          id: args.name + "-id",
          state: {
            ...args.inputs,
            ...(args.name === "portals-access" && {
              aud: "issued-portal-audience",
            }),
          },
        };
      },
      call(args) {
        if (args.token === "cloudflare:index/getZone:getZone")
          return { id: "zone-id", account: { id: "account-id" } };
        if (args.token === "cloudflare:index/getDnsRecords:getDnsRecords")
          return {
            results: [
              {
                name: "*.raygen.dev",
                type: "CNAME",
                proxied: true,
                content: "homelab-tunnel.cfargotunnel.com",
              },
            ],
          };
        throw new Error("Unexpected invoke: " + args.token);
      },
    },
    "pulumi-homelab",
    "test",
  );
  const monitor = getMonitor();
  const registerResource = monitor.registerResource.bind(monitor);
  monitor.registerResource = (request, callback) => {
    dependencies.set(request.getName(), request.getDependenciesList());
    return registerResource(request, callback);
  };
  const ready = new pulumi.CustomResource("test:index:Ready", "ready", {});
  const { register } = require("../bin/external/portals");
  const result = register({
    cloudflareProvider: new cloudflare.Provider("cf", {
      apiToken: "test-token",
    }),
    consulProvider: new consul.Provider("consul", {
      address: "localhost:8500",
    }),
    cloudflaredTunnelToken: Buffer.from('{"t":"homelab-tunnel"}').toString(
      "base64",
    ),
    sshPrivateKey: pulumi.secret("test-key"),
    commands: new Map([
      ["traefik-setup", ready],
      ["amp-runner-setup", ready],
    ]),
  });
  await result.id.promise();
  const app = resources.get("portals-access").inputs;
  assert.equal(app.domain, "portals.raygen.dev");
  assert.deepEqual(app.policies[0].includes, [
    { email: { email: "raygenrrupe@gmail.com" } },
  ]);
  assert.equal(app.policies[0].decision, "allow");
  const deploy = resources.get("portals-install").inputs;
  const config = JSON.parse(deploy.stdin);
  assert.equal(config.accessAudience, "issued-portal-audience");
  assert.equal(config.accessIssuer, "https://bagel.cloudflareaccess.com");
  assert.equal(config.workspaceRoot, "/home/amp/workspaces");
  assert.equal(config.port, 4310);
  assert.equal(
    config.signingKey,
    undefined,
    "signing key is generated and retained locally, not in deploy logs",
  );
  const connection = deploy.connection.value;
  assert.equal(connection.host, "192.168.0.214");
  assert.match(connection.hostKey, /^ssh-ed25519 /);
  const route = resources.get("portals-service").inputs;
  assert.equal(route.port, 4310);
  assert.equal(route.address, "192.168.0.214");
  assert.ok(
    route.tags.includes(
      "traefik.http.routers.portals.rule=Host(`portals.raygen.dev`) || HostRegexp(`^p-[a-f0-9]{16}\\.raygen\\.dev$`)",
    ),
  );
  assert.ok(
    route.tags.includes(
      "traefik.http.middlewares.portals-tunnel-only.ipallowlist.sourcerange=192.168.0.204/32",
    ),
  );
  for (const name of ["portals-install", "portals-dns"])
    assert.ok(
      dependencies
        .get("portals-service")
        .some((urn) => urn.endsWith("::" + name)),
    );
  assert.ok(
    dependencies
      .get("portals-dns")
      .some((urn) => urn.endsWith("::portals-access")),
  );
  assert.equal(resources.has("oracle-setup"), false);
  assert.equal(
    [...resources.values()].some((r) => /Workers|Certificate/.test(r.type)),
    false,
  );
});
