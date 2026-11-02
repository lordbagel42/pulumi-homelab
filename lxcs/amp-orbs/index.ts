import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ip, ServiceContext } from "../../framework";
import { lxcPassword, optionalSecret, readSecret } from "../../infisical";

export const name = "amp-orbs";
export const provides = ["amp-orbs-setup"];

export function register(ctx: ServiceContext): void {
  const config = new pulumi.Config(name);
  if (!config.getBoolean("enabled")) return;
  const secrets = { ...ctx.infisicalConfig, secretPath: "/amp-orbs" };
  const address = ip(218);
  const rootPassword = lxcPassword(name, secrets);
  const machine = new ProxmoxMachine(
    name,
    {
      type: "lxc",
      nodeName: "optiplex",
      vmId: 218,
      ip: address,
      cpu: 1,
      cpuLimit: 1,
      memory: 768,
      swap: 256,
      disk: 4,
      nesting: true,
      tags: ["amp", "e2b"],
      sshKeys: [ctx.sshKey],
      password: rootPassword,
    },
    {
      provider: ctx.provider,
      protect: true,
      // Proxmox accepts this only on creation. Rotate in the provisioner,
      // never replace the controller just to change its root password.
      ignoreChanges: ["initialization.userAccount.password"],
    },
  );

  // Enroll this public key through the trusted Optiplex console first.
  const hostKey = config.get("sshHostKey");
  if (!hostKey) return;
  const files = Object.fromEntries(
    [
      "provision.py",
      "dispatch.mjs",
      "amp-orbs.service",
      "package.json",
      "pnpm-lock.yaml",
    ].map((file) => [
      file,
      fs.readFileSync(path.join(__dirname, file), "utf8"),
    ]),
  );
  const payload = pulumi
    .all([
      readSecret("E2B_API_KEY", secrets),
      optionalSecret("AMP_ORB_PROVIDER_TOKEN", secrets),
      rootPassword,
    ])
    .apply(([apiKey, providerToken, rootPassword]) =>
      JSON.stringify({
        files,
        apiKey,
        providerToken,
        rootPassword,
        template: config.get("template") ?? "amp-orbs",
      }),
    );
  const install = `set -eu
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates </dev/null
fi
python3 -c 'import json,sys; payload=json.load(sys.stdin); namespace={"__name__":"provisioner"}; exec(compile(payload["files"]["provision.py"], "provision.py", "exec"), namespace); namespace["main"](payload)'
`;
  const provision = new command.remote.Command(
    "amp-orbs-provision",
    {
      connection: {
        host: address,
        user: "root",
        privateKey: ctx.sshPrivateKey,
        hostKey,
        dialErrorLimit: 20,
        perDialTimeout: 10,
      },
      create: install,
      update: install,
      stdin: payload,
      logging: "none",
      addPreviousOutputInEnv: false,
    },
    {
      parent: machine,
      dependsOn: [machine.machine],
      additionalSecretOutputs: ["stdout", "stderr"],
      customTimeouts: { create: "15m", update: "15m" },
    },
  );
  ctx.commands.set("amp-orbs-setup", provision);
}
