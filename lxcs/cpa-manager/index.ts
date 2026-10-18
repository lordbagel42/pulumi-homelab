import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ServiceContext } from "../../framework";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { managedSecret } from "../../infisical";

export const name = "cpa-manager";
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    if (!ctx.cloudflareProvider || !ctx.consulProvider)
        throw new Error("CPA routing requires Cloudflare and Consul");
    const domain = "codex.mrow.tech";
    tunnelHostname(name, {
        domain,
        zone: "mrow.tech",
        provider: ctx.cloudflareProvider,
        tunnelToken: ctx.cloudflaredTunnelToken,
    });
    // The shared secret helper returns stdout; suppress it at the resource too.
    pulumi.runtime.registerStackTransformation((args) => {
        if (
            args.type !== "command:local:Command" ||
            !args.name.startsWith("infisical-secret-CPA_MANAGER_")
        )
            return;
        return {
            props: { ...args.props, logging: "none" },
            opts: {
                ...args.opts,
                additionalSecretOutputs: ["stdout", "stderr"],
            },
        };
    });
    const secrets = { ...ctx.infisicalConfig, secretPath: "/cpa-manager" };
    const machine = new ProxmoxMachine(
        name,
        {
            type: "lxc",
            nodeName: "optiplex",
            vmId: 217,
            ip: "192.168.0.217",
            cpu: 1,
            cpuLimit: 1,
            memory: 1024,
            swap: 256,
            disk: 12,
            datastoreId: "local-lvm",
            nesting: true,
            tags: ["cpa-manager"],
            sshKeys: [ctx.sshKey],
            password: managedSecret("CPA_MANAGER_ROOT_PASSWORD", secrets),
            consulProvider: ctx.consulProvider,
            reverseProxy: {
                domain,
                port: 18317,
                // Both backends enforce their own keys; no interactive SSO on API traffic.
                extraTags: [
                    "traefik.http.routers.cpa-manager.service=cpa-manager",
                    `traefik.http.routers.cpa-gateway.rule=Host(\`${domain}\`) && (Path(\`/v1\`) || PathPrefix(\`/v1/\`))`,
                    "traefik.http.routers.cpa-gateway.entrypoints=web",
                    "traefik.http.routers.cpa-gateway.service=cpa-gateway",
                    "traefik.http.services.cpa-gateway.loadbalancer.server.port=8317",
                ],
            },
        },
        { provider: ctx.provider, protect: true },
    );

    // Enroll the guest's host key through trusted Proxmox SSH before provisioning.
    const hostKey = config.get("sshHostKey");
    if (!hostKey) return;
    const read = (file: string) =>
        fs.readFileSync(path.join(__dirname, file), "utf8");
    const provisioner = read("provision.py");
    const compose = read("compose.yaml");
    const payload = pulumi
        .all([
            managedSecret("CPA_MANAGER_ADMIN_KEY", secrets),
            managedSecret("CPA_MANAGER_DATA_KEY", secrets),
            managedSecret("CPA_MANAGER_UPSTREAM_KEY", secrets),
            managedSecret("CPA_MANAGER_CLIENT_KEY", secrets),
        ])
        .apply(([adminKey, dataKey, upstreamKey, clientKey]) =>
            JSON.stringify({
                adminKey,
                dataKey,
                upstreamKey,
                clientKey,
                compose,
                provisioner,
            }),
        );
    const install = `set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq </dev/null
apt-get install -y --no-install-recommends python3 ca-certificates docker.io docker-cli docker-compose </dev/null
apt-get clean
python3 -c 'import json,sys; p=json.load(sys.stdin); n={"__name__":"provisioner"}; exec(compile(p.pop("provisioner"), "provision.py", "exec"), n); n["main"](p)'
`;
    new command.remote.Command(
        "cpa-manager-provision",
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
            customTimeouts: { create: "20m", update: "20m" },
        },
    );
}
