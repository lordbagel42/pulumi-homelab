import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as cloudflare from "@pulumi/cloudflare";
import * as consul from "@pulumi/consul";
import { tunnelId } from "../../framework/cloudflare-dns";
import type { ServiceContext } from "../../framework";

export const name = "portals";
export const provides = ["portals-setup"];
export const dependencies = [
  "amp-runner-setup",
  "consul-setup",
  "traefik-setup",
];

export function register(ctx: ServiceContext): consul.Service {
  if (!ctx.cloudflareProvider || !ctx.consulProvider)
    throw new Error("Portals requires Cloudflare and Consul providers");
  const cf = ctx.cloudflareProvider;
  const zone = cloudflare.getZoneOutput(
    {
      filter: { name: pulumi.unsecret(cf.apiToken.apply(() => "raygen.dev")) },
    },
    { provider: cf },
  );
  const tunnel = tunnelId(ctx.cloudflaredTunnelToken);
  const wildcard = cloudflare.getDnsRecordsOutput(
    { zoneId: zone.id, name: { exact: "*.raygen.dev" } },
    { provider: cf },
  );
  const destination = pulumi.all([wildcard, tunnel]).apply(([records, id]) => {
    const expected = `${id}.cfargotunnel.com`;
    if (
      !records.results.some(
        (r) =>
          r.name === "*.raygen.dev" &&
          r.type === "CNAME" &&
          r.proxied &&
          r.content === expected,
      )
    ) {
      throw new Error(
        "Portals requires existing proxied *.raygen.dev DNS to the homelab tunnel; refusing to change unrelated wildcard DNS",
      );
    }
    return expected;
  });
  const access = new cloudflare.ZeroTrustAccessApplication(
    "portals-access",
    {
      accountId: zone.account.apply((a) => a.id),
      name: "Raygen portals",
      domain: "portals.raygen.dev",
      destinations: [{ type: "public", uri: "portals.raygen.dev" }],
      type: "self_hosted",
      sessionDuration: "8h",
      httpOnlyCookieAttribute: true,
      policies: [
        {
          name: "Raygen only",
          decision: "allow",
          precedence: 1,
          includes: [{ email: { email: "raygenrrupe@gmail.com" } }],
        },
      ],
    },
    { provider: cf },
  );
  const dns = new cloudflare.DnsRecord(
    "portals-dns",
    {
      zoneId: zone.id,
      name: "portals.raygen.dev",
      type: "CNAME",
      content: destination,
      proxied: true,
      ttl: 1,
      comment: "managed by pulumi-homelab",
    },
    { provider: cf, dependsOn: [access] },
  );

  const connection = {
    host: "192.168.0.214",
    user: "root",
    privateKey: ctx.sshPrivateKey,
    // Verified directly from this runner's /etc/ssh/ssh_host_ed25519_key.pub.
    hostKey:
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDZRrbCIWuMrhOYbMmT/3c3DJ689im6kOQIKc8aA2NQf",
    dialErrorLimit: 2,
    perDialTimeout: 10,
  };
  // Assets resolve from the Pulumi project root, both under ts-node and in tests.
  const directory = path.resolve("external/portals");
  const archive = path.join(directory, "raygen-portals.tgz");
  const digest = crypto
    .createHash("sha256")
    .update(fs.readFileSync(archive))
    .digest("hex");
  const bundle = new command.remote.CopyToRemote(
    "portals-bundle",
    {
      connection,
      source: new pulumi.asset.FileAsset(archive),
      remotePath: "/opt/raygen-portals/release.tgz",
      triggers: [digest],
    },
    { dependsOn: ctx.commands.get("amp-runner-setup") },
  );
  const config = access.aud.apply((audience) =>
    JSON.stringify({
      stateDir: "/var/lib/raygen-portals",
      socketPath: "/run/raygen-portals/control.sock",
      bindHost: "192.168.0.214",
      port: 4310,
      workspaceRoot: "/home/amp/workspaces",
      domain: "raygen.dev",
      loginHost: "portals.raygen.dev",
      accessIssuer: "https://bagel.cloudflareaccess.com",
      accessAudience: audience,
      allowedEmail: "raygenrrupe@gmail.com",
    }),
  );
  const script = fs
    .readFileSync(path.join(directory, "install.sh"), "utf8")
    .replace(/__RELEASE_SHA__/g, digest);
  const install = new command.remote.Command(
    "portals-install",
    {
      connection,
      create: `bash -c '${script.replace(/'/g, `'"'"'`)}'`,
      stdin: config,
      triggers: [digest, script, config],
      addPreviousOutputInEnv: false,
    },
    { dependsOn: [bundle, dns] },
  );
  ctx.commands.set("portals-setup", install);
  const node = new consul.Node(
    "portals-node",
    {
      name: "portals-svc",
      address: "192.168.0.214",
    },
    { provider: ctx.consulProvider },
  );
  return new consul.Service(
    "portals-service",
    {
      name,
      node: node.name,
      address: "192.168.0.214",
      port: 4310,
      tags: [
        "traefik.enable=true",
        "traefik.http.routers.portals.rule=Host(`portals.raygen.dev`) || HostRegexp(`^p-[a-f0-9]{16}\\.raygen\\.dev$`)",
        "traefik.http.routers.portals.priority=200",
        "traefik.http.routers.portals.entrypoints=web",
        "traefik.http.services.portals.loadbalancer.server.port=4310",
        "traefik.http.routers.portals.middlewares=portals-tunnel-only@consulcatalog",
        "traefik.http.middlewares.portals-tunnel-only.ipallowlist.sourcerange=192.168.0.204/32",
      ],
    },
    {
      provider: ctx.consulProvider,
      dependsOn: [
        node,
        install,
        dns,
        ...[ctx.commands.get("traefik-setup")].filter(
          (r): r is pulumi.Resource => !!r,
        ),
      ],
    },
  );
}
