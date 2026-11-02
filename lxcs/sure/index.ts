import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import type { ServiceContext } from "../../framework";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import { managedSecret, readSecret } from "../../infisical";

export const name = "sure";
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    if (!ctx.cloudflareProvider || !ctx.consulProvider)
        throw new Error("Sure routing requires Cloudflare and Consul");

    const domain = "sure.raygen.dev";
    const dns = tunnelHostname(name, {
        domain,
        zone: "raygen.dev",
        provider: ctx.cloudflareProvider,
        tunnelToken: ctx.cloudflaredTunnelToken,
        // Protect first-admin onboarding and financial data at the edge.
        accessEmails: [config.require("ownerEmail")],
    });
    const secrets = { ...ctx.infisicalConfig, secretPath: "/sure" };
    const machine = new ProxmoxMachine(
        name,
        {
            type: "lxc",
            nodeName: "optiplex",
            vmId: 219,
            ip: "192.168.0.219",
            cpu: 2,
            cpuLimit: 2,
            memory: 2048,
            swap: 512,
            disk: 24,
            // Optiplex's local-lvm is 90% full; hdd has ample capacity.
            datastoreId: "hdd",
            nesting: true,
            tags: ["sure"],
            sshKeys: [ctx.sshKey],
            password: managedSecret("SURE_ROOT_PASSWORD", secrets),
            consulProvider: ctx.consulProvider,
            reverseProxy: { domain, port: 3000 },
        },
        { provider: ctx.provider, protect: true, dependsOn: [dns] },
    );

    // Enroll from the authenticated Proxmox host before connecting to the guest.
    const hostKey = config.get("sshHostKey");
    if (!hostKey) return;
    const read = (file: string) =>
        fs.readFileSync(path.join(__dirname, file), "utf8");
    const payload = pulumi
        .all([
            managedSecret("SURE_SECRET_KEY_BASE", secrets),
            managedSecret("SURE_POSTGRES_PASSWORD", secrets),
            managedSecret("SURE_ENCRYPTION_PRIMARY_KEY", secrets),
            managedSecret("SURE_ENCRYPTION_DETERMINISTIC_KEY", secrets),
            managedSecret("SURE_ENCRYPTION_KEY_DERIVATION_SALT", secrets),
            config.getBoolean("aiEnabled")
                ? readSecret("SURE_HACKCLUB_AI_KEY", secrets)
                : pulumi.output(""),
        ])
        .apply(
            ([
                secretKey,
                dbPassword,
                primaryKey,
                deterministicKey,
                salt,
                aiKey,
            ]) =>
                JSON.stringify({
                    env: {
                        SECRET_KEY_BASE: secretKey.trim(),
                        POSTGRES_PASSWORD: dbPassword.trim(),
                        ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY: primaryKey.trim(),
                        ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY:
                            deterministicKey.trim(),
                        ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT:
                            salt.trim(),
                        OPENAI_ACCESS_TOKEN: aiKey.trim(),
                        JEV_API_KEY: config.getBoolean("jevEnabled")
                            ? aiKey.trim()
                            : "",
                        CATEGORIZATION_PROVIDER: config.getBoolean("jevEnabled")
                            ? "jev"
                            : "llm",
                    },
                    compose: read("compose.yaml"),
                    provisioner: read("provision.py"),
                }),
        );
    const install = `set -eu
export DEBIAN_FRONTEND=noninteractive
# The host's NetBird resolver is not reachable from this LAN-only guest.
touch /etc/.pve-ignore.resolv.conf
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf
apt-get update -qq </dev/null
apt-get install -y --no-install-recommends python3 ca-certificates docker.io docker-cli docker-compose </dev/null
apt-get clean
python3 -c 'import json,sys; p=json.load(sys.stdin); n={"__name__":"provisioner"}; exec(compile(p.pop("provisioner"), "provision.py", "exec"), n); n["main"](p)'
`;
    new command.remote.Command(
        "sure-provision",
        {
            connection: {
                host: machine.ip,
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
            protect: false,
            dependsOn: [machine.machine],
            additionalSecretOutputs: ["stdout", "stderr"],
            customTimeouts: { create: "30m", update: "20m" },
        },
    );
}
