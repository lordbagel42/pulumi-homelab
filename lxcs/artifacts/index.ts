import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ip, ServiceContext } from "../../framework";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { readSecret } from "../../infisical";

export const name = "artifacts";
export const provides = ["artifacts-setup"];
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    const secrets = { ...ctx.infisicalConfig, secretPath: "/artifacts" };
    const vmId = config.getNumber("vmId") ?? 216;
    const memory = config.getNumber("memoryMiB") ?? 1024;
    const cpuLimit = config.getNumber("cpuLimit") ?? 1;
    if (!Number.isInteger(vmId) || vmId < 100 || vmId > 254)
        throw new Error("artifacts:vmId must be 100..254 for ip(id)");
    if (!Number.isInteger(memory) || memory < 256)
        throw new Error("artifacts:memoryMiB must be an integer >= 256");
    if (!Number.isFinite(cpuLimit) || cpuLimit <= 0 || cpuLimit > 1)
        throw new Error("artifacts:cpuLimit must be > 0 and <= 1");
    const address = ip(vmId);
    const machine = new ProxmoxMachine(
        name,
        {
            type: "lxc",
            nodeName: "optiplex",
            vmId,
            ip: address,
            cpu: 1,
            cpuLimit,
            memory,
            disk: 8,
            nesting: true,
            tags: ["artifacts"],
            sshKeys: [ctx.sshKey],
            password: readSecret("ARTIFACTS_ROOT_PASSWORD", secrets),
        },
        { provider: ctx.provider, protect: true },
    );

    // Stage one creates only the protected LXC. Enroll through pct on optiplex.
    const hostKey = config.get("sshHostKey");
    if (!hostKey) return;
    const bundle = path.resolve(config.require("bundlePath"));
    if (
        !fs.lstatSync(bundle).isDirectory() ||
        !/^[A-Za-z0-9._-]+$/.test(path.basename(bundle))
    ) {
        throw new Error(
            "artifacts:bundlePath must be a real private directory with a simple basename",
        );
    }
    if ((fs.statSync(bundle).mode & 0o077) !== 0)
        throw new Error("artifacts:bundlePath must be private (chmod 0700)");
    const binary = path.join(bundle, "artifacts");
    if (
        fs.readdirSync(bundle).join(",") !== "artifacts" ||
        !fs.lstatSync(binary).isFile()
    ) {
        throw new Error(
            "artifacts:bundlePath must contain only the regular file artifacts (no symlinks)",
        );
    }
    const hash = crypto
        .createHash("sha256")
        .update(fs.readFileSync(binary))
        .digest("hex");
    const staging = `/opt/artifacts/incoming/${hash}`;
    const connection = {
        host: address,
        user: "root",
        privateKey: ctx.sshPrivateKey,
        hostKey,
        dialErrorLimit: 20,
        perDialTimeout: 10,
    };
    const prepareScript = `set -eu
umask 077
install -d -m 0700 ${staging}
if ! command -v python3 >/dev/null 2>&1; then
  apt-get update -qq </dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates </dev/null
fi
`;
    const prepare = new command.remote.Command(
        "artifacts-prepare",
        {
            connection,
            create: prepareScript,
            update: prepareScript,
            triggers: [hash],
            logging: "none",
            addPreviousOutputInEnv: false,
        },
        {
            parent: machine,
            protect: false,
            dependsOn: [machine.machine],
            additionalSecretOutputs: ["stdout", "stderr"],
        },
    );
    const copy = new command.remote.CopyToRemote(
        "artifacts-bundle",
        {
            connection,
            source: new pulumi.asset.FileArchive(bundle),
            remotePath: staging,
            triggers: [hash],
        },
        { parent: machine, protect: false, dependsOn: [prepare] },
    );
    const read = (file: string): string =>
        fs.readFileSync(path.join(__dirname, file), "utf8");
    const files = Object.fromEntries(
        [
            "artifacts.service",
            "artifacts-consul.service",
            "artifacts-usage.service",
            "artifacts-usage.timer",
            "usage.py",
        ].map((file) => [file, read(file)]),
    );
    const provisioner = read("provision.py");
    const ownerEmail = config.require("ownerEmail");
    const payload = pulumi
        .all([
            readSecret("ARTIFACTS_AUTH_SECRET", secrets),
            readSecret("ARTIFACTS_OWNER_PASSWORD", secrets),
        ])
        .apply(([authSecret, ownerPassword]) =>
            JSON.stringify({
                authSecret,
                ownerPassword,
                ownerEmail,
                address,
                hash,
                binary: `${staging}/${path.basename(bundle)}/artifacts`,
                files,
                provisioner,
            }),
        );
    const install =
        'python3 -c \'import json,sys; p=json.load(sys.stdin); n={"__name__":"provisioner"}; exec(compile(p.pop("provisioner"), "provision.py", "exec"), n); n["main"](p)\'';
    const provision = new command.remote.Command(
        "artifacts-provision",
        {
            connection,
            create: install,
            update: install,
            stdin: payload,
            triggers: [hash],
            logging: "none",
            addPreviousOutputInEnv: false,
        },
        {
            parent: machine,
            protect: false,
            dependsOn: [
                copy,
                ...(ctx.commands.has("consul-setup")
                    ? [ctx.commands.get("consul-setup")!]
                    : []),
            ],
            additionalSecretOutputs: ["stdout", "stderr"],
            customTimeouts: { create: "15m", update: "15m" },
        },
    );
    ctx.commands.set("artifacts-setup", provision);
}
