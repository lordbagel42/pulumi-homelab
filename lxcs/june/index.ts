import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as command from "@pulumi/command";
import * as consul from "@pulumi/consul";
import * as pulumi from "@pulumi/pulumi";
import { ip, ServiceContext } from "../../framework";
import { tunnelHostname } from "../../framework/cloudflare-dns";
import { ProxmoxMachine } from "../../framework/proxmox-machine";

export const name = "june";
export const provides = ["june-setup"];
export const dependencies = ["consul-setup"];

const juneConfig = {
    host: "192.168.0.215",
    port: 3080,
    setupMode: true,
    owner: { id: "raygen", identities: [] },
    model: {
        protocol: "codex",
        model: "gpt-6-astra",
        home: "/var/lib/june/.codex",
        executable: "/opt/june/current/node_modules/.bin/codex",
    },
    coding: { enabled: false },
    console: { origin: "http://127.0.0.1:3080" },
};

function readSourceArchive(config: pulumi.Config): { archive: string; revision: string } {
    const archive = path.resolve(config.require("sourceArchive"));
    const metadata = fs.lstatSync(archive);
    if (!metadata.isFile() || metadata.isSymbolicLink() || !archive.endsWith(".tar.gz")) {
        throw new Error("june:sourceArchive must be a regular local .tar.gz file");
    }
    const revision = crypto.createHash("sha256").update(fs.readFileSync(archive)).digest("hex");
    const expectedRevision = config.get("sourceArchiveSha256");
    if (expectedRevision && expectedRevision.toLowerCase() !== revision) {
        throw new Error("june:sourceArchiveSha256 does not match june:sourceArchive");
    }
    return { archive, revision };
}

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;

    const slack = config.getObject<{ teamId: string; botUserId: string; ownerUserId: string }>("slack");
    if (slack) {
        if (!/^T[A-Z0-9]+$/.test(slack.teamId) ||
            !/^[UW][A-Z0-9]+$/.test(slack.botUserId) ||
            !/^[UW][A-Z0-9]+$/.test(slack.ownerUserId) ||
            slack.botUserId === slack.ownerUserId || Object.keys(slack).length !== 3) {
            throw new Error("june:slack requires a workspace teamId and distinct botUserId/ownerUserId");
        }
        if (!ctx.consulProvider || !ctx.cloudflareProvider) {
            throw new Error("Slack ingress requires the Consul and Cloudflare providers");
        }
    }
    const runtimeConfig = slack ? {
        ...juneConfig,
        setupMode: false,
        owner: { id: "raygen", identities: [{ channel: "slack", accountId: slack.teamId, senderId: slack.ownerUserId }] },
        slack: {
            teamId: slack.teamId,
            botUserId: slack.botUserId,
            signingSecretEnv: "SLACK_SIGNING_SECRET",
            botTokenEnv: "SLACK_BOT_TOKEN",
        },
    } : juneConfig;

    const address = ip(215);
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        nodeName: "optiplex",
        vmId: 215,
        ip: address,
        cpu: 2,
        memory: 2048,
        disk: 12,
        // Debian systemd hardening and Rivet's child process need mount and PID
        // namespaces while the container itself remains unprivileged.
        nesting: true,
        tags: ["ai", "june"],
        sshKeys: [ctx.sshKey],
        password: config.requireSecret("rootPassword"),
    }, { provider: ctx.provider, protect: true });

    // Create the machine first. Provisioning is deliberately absent until the
    // host key has been read through the Proxmox console and pinned in config.
    const hostKey = config.get("sshHostKey")?.trim();
    if (!hostKey) return;
    if (!/^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: .*)?$/.test(hostKey)) {
        throw new Error("june:sshHostKey must be the verified OpenSSH Ed25519 public host key");
    }

    const { archive, revision } = readSourceArchive(config);
    const remoteArchive = `/opt/june/incoming/${revision}.tar.gz`;
    const connection = {
        host: address,
        user: "root",
        privateKey: ctx.sshPrivateKey,
        hostKey,
        dialErrorLimit: 30,
        perDialTimeout: 10,
    };
    const stageScript = "install -d -o root -g root -m 0700 /opt/june/incoming";
    const stage = new command.remote.Command("june-stage", {
        connection,
        create: stageScript,
        update: stageScript,
        triggers: [revision],
        logging: "none",
        addPreviousOutputInEnv: false,
    }, {
        parent: machine,
        // Only the LXC is protected. These commands have no remote delete action;
        // source-digest changes replace their bookkeeping, not the machine/state.
        protect: false,
        dependsOn: [machine.machine],
        additionalSecretOutputs: ["stdout", "stderr"],
    });
    const upload = new command.remote.CopyToRemote("june-source", {
        connection,
        source: new pulumi.asset.FileAsset(archive),
        remotePath: remoteArchive,
        triggers: [revision],
    }, { parent: machine, protect: false, dependsOn: [stage] });

    const read = (file: string): string => fs.readFileSync(path.join(__dirname, file), "utf8");
    const payload = pulumi.all({
        operatorToken: config.requireSecret("operatorToken"),
        botToken: slack ? config.requireSecret("slackBotToken") : undefined,
        signingSecret: slack ? config.requireSecret("slackSigningSecret") : undefined,
    }).apply(({ operatorToken, botToken, signingSecret }) => JSON.stringify({
        archive: remoteArchive,
        revision,
        operatorToken,
        slack: slack ? { ...slack, botToken, signingSecret } : undefined,
        config: JSON.stringify(runtimeConfig, null, 2) + "\n",
        provisioner: read("provision.py"),
        service: read("june.service"),
        startLogin: read("start-login.sh"),
    }));
    const install = `set -eu
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates xz-utils </dev/null
fi
python3 -c 'import json,sys; payload=json.load(sys.stdin); namespace={"__name__":"provisioner"}; exec(compile(payload.pop("provisioner"), "provision.py", "exec"), namespace); namespace["main"](payload)'
`;
    const provision = new command.remote.Command("june-provision", {
        connection,
        create: install,
        update: install,
        stdin: payload,
        triggers: [revision, payload],
        logging: "none",
        addPreviousOutputInEnv: false,
    }, {
        parent: machine,
        protect: false,
        dependsOn: [upload],
        additionalSecretOutputs: ["stdout", "stderr"],
        customTimeouts: { create: "30m", update: "30m" },
    });
    ctx.commands.set("june-setup", provision);

    if (slack && ctx.consulProvider && ctx.cloudflareProvider) {
        const domain = "june-slack.bagelindustries.com";
        tunnelHostname("june-slack", {
            domain,
            tunnelToken: ctx.cloudflaredTunnelToken,
            provider: ctx.cloudflareProvider,
        });
        const node = new consul.Node("june-slack-node", {
            name: "june-slack-svc",
            address,
        }, { provider: ctx.consulProvider });
        // Never use the host-only reverseProxy shortcut: it would also publish
        // the operator API. Slack authenticates this exact endpoint by HMAC.
        new consul.Service("june-slack-service", {
            name: "june-slack",
            node: node.name,
            address,
            port: 3080,
            tags: [
                "traefik.enable=true",
                `traefik.http.routers.june-slack.rule=Host(\`${domain}\`) && Path(\`/webhooks/slack\`) && Method(\`POST\`)`,
                "traefik.http.routers.june-slack.entrypoints=web",
                "traefik.http.routers.june-slack.service=june-slack",
                "traefik.http.services.june-slack.loadbalancer.server.port=3080",
            ],
        }, { provider: ctx.consulProvider, dependsOn: [node, provision] });
    }
}
