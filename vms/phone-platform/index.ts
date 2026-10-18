import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as consul from "@pulumi/consul";
import { ServiceContext } from "../../framework";
import { readSecret } from "../../infisical";

export const name = "phone-platform";
export const provides = ["phone-platform-setup"];
export const dependencies = ["phone-voice-setup", "consul-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    const secrets = { ...ctx.infisicalConfig, secretPath: "/phone-platform" };
    const voice = ctx.commands.get("phone-voice-setup");
    if (!voice)
        throw new Error(
            "phone-platform requires the existing phone-voice deployment",
        );
    if (!ctx.consulProvider)
        throw new Error("phone-platform requires the homelab Consul provider");
    const connection = {
        host: "192.168.0.233",
        user: "root",
        privateKey: ctx.sshPrivateKey,
        hostKey: new pulumi.Config("huddle-phone").require("sshHostKey"),
        dialErrorLimit: 30,
        perDialTimeout: 10,
    };
    const source = path.join(__dirname, "source");
    const hash = crypto.createHash("sha256");
    function visit(dir: string): void {
        for (const entry of fs
            .readdirSync(dir, { withFileTypes: true })
            .sort((a, b) => a.name.localeCompare(b.name))) {
            if (
                entry.isSymbolicLink() ||
                [
                    "state",
                    "models",
                    ".venv",
                    ".env",
                    "node_modules",
                    "__pycache__",
                    "admin-token",
                ].includes(entry.name)
            ) {
                throw new Error(
                    `Private or generated file in platform source: ${entry.name}`,
                );
            }
            const file = path.join(dir, entry.name);
            if (entry.isDirectory()) visit(file);
            else
                hash.update(path.relative(source, file))
                    .update("\0")
                    .update(fs.readFileSync(file))
                    .update("\0");
        }
    }
    visit(source);
    const revision = hash.digest("hex");
    const release = `/opt/phone-platform/releases/${revision}`;
    const stageScript = `install -d -m 0755 '${release}'`;
    const stage = new command.remote.Command(
        "phone-platform-stage",
        {
            connection,
            create: stageScript,
            update: stageScript,
            addPreviousOutputInEnv: false,
        },
        { dependsOn: [voice] },
    );
    const upload = new command.remote.CopyToRemote(
        "phone-platform-source",
        {
            connection,
            source: new pulumi.asset.FileArchive(source),
            remotePath: release,
            triggers: [revision],
        },
        { dependsOn: [stage] },
    );
    const read = (file: string) =>
        fs.readFileSync(path.join(source, "deploy", file), "utf8");
    const payload = pulumi
        .output({
            release: `${release}/source`,
            adminToken: readSecret("PHONE_PLATFORM_ADMIN_TOKEN", secrets),
            phoneToken: readSecret("PHONE_PLATFORM_PHONE_TOKEN", secrets),
            huddleSecret: readSecret("HUDDLE_PHONE_OPERATOR_SECRET", {
                ...ctx.infisicalConfig,
                secretPath: "/huddle-phone",
            }),
            huddleOwnerId:
                new pulumi.Config("huddle-phone").get("operatorOwnerId") ?? "",
            calls: pulumi.all({
                channel:
                    new pulumi.Config("huddle-phone").get("phoneChannel") ??
                    "SCCP/6738",
                user: "huddle-phone",
                secret: readSecret("HUDDLE_PHONE_AMI_SECRET", {
                    ...ctx.infisicalConfig,
                    secretPath: "/huddle-phone",
                }),
            }),
            service: read("phone-platform.service"),
        })
        .apply((value) => JSON.stringify(value));
    const provision = `python3 -c '${read("provision.py").replace(/'/g, "'\\''")}'`;
    const setup = new command.remote.Command(
        "phone-platform-setup",
        {
            connection,
            create: provision,
            update: provision,
            stdin: payload,
            logging: "none",
            triggers: [revision, payload],
            addPreviousOutputInEnv: false,
        },
        {
            dependsOn: [upload],
            additionalSecretOutputs: ["stdout", "stderr"],
            customTimeouts: { create: "15m", update: "15m" },
        },
    );
    ctx.commands.set("phone-platform-setup", setup);

    // Existing wildcard DNS/TLS and the homelab tunnel already reach Traefik.
    // Publish only the provider WebSocket; Switchboard verifies its JWT before
    // accepting it. The dashboard and admin API remain LAN-only.
    const speechNode = new consul.Node(
        "phone-platform-speech-engine-node",
        {
            name: "phone-platform-speech-engine-svc",
            address: connection.host,
        },
        { provider: ctx.consulProvider, dependsOn: [setup] },
    );
    new consul.Service(
        "phone-platform-speech-engine-service",
        {
            name: "phone-speech-engine",
            node: speechNode.name,
            address: connection.host,
            port: 8088,
            tags: [
                "traefik.enable=true",
                "traefik.http.routers.phone-speech-engine.rule=Host(`speech.raygen.dev`) && Path(`/speech-engine/upstream`) && Method(`GET`) && HeaderRegexp(`Upgrade`, `(?i)^websocket$`)",
                "traefik.http.routers.phone-speech-engine.entrypoints=web",
                "traefik.http.routers.phone-speech-engine.priority=300",
                "traefik.http.routers.phone-speech-engine.service=phone-speech-engine",
                "traefik.http.routers.phone-speech-engine.middlewares=phone-speech-engine-tunnel",
                "traefik.http.routers.phone-speech-engine.observability.accesslogs=false",
                "traefik.http.routers.phone-speech-engine.observability.tracing=false",
                "traefik.http.services.phone-speech-engine.loadbalancer.server.port=8088",
                "traefik.http.middlewares.phone-speech-engine-tunnel.ipallowlist.sourcerange=192.168.0.204/32",
                // Override the shared Authentik outpost router (priority 100) for
                // every other path/method on this dedicated provider hostname.
                "traefik.http.routers.phone-speech-engine-deny.rule=Host(`speech.raygen.dev`)",
                "traefik.http.routers.phone-speech-engine-deny.entrypoints=web",
                "traefik.http.routers.phone-speech-engine-deny.priority=200",
                "traefik.http.routers.phone-speech-engine-deny.service=noop@internal",
                "traefik.http.routers.phone-speech-engine-deny.observability.accesslogs=false",
                "traefik.http.routers.phone-speech-engine-deny.observability.tracing=false",
            ],
        },
        { provider: ctx.consulProvider, dependsOn: [speechNode] },
    );
    new consul.Service(
        "phone-platform-calls-service",
        {
            name: "phone-calls",
            node: speechNode.name,
            address: connection.host,
            port: 8088,
            tags: [
                "traefik.enable=true",
                "traefik.http.routers.phone-calls.rule=Host(`calls.raygen.dev`) && (Path(`/api/v1/calls`) || PathPrefix(`/api/v1/calls/`)) && (Method(`GET`) || Method(`POST`) || Method(`DELETE`))",
                "traefik.http.routers.phone-calls.entrypoints=web",
                "traefik.http.routers.phone-calls.priority=300",
                "traefik.http.routers.phone-calls.service=phone-calls",
                "traefik.http.routers.phone-calls.middlewares=phone-calls-tunnel",
                "traefik.http.routers.phone-calls.observability.accesslogs=false",
                "traefik.http.routers.phone-calls.observability.tracing=false",
                "traefik.http.services.phone-calls.loadbalancer.server.port=8088",
                "traefik.http.middlewares.phone-calls-tunnel.ipallowlist.sourcerange=192.168.0.204/32",
                "traefik.http.routers.phone-calls-deny.rule=Host(`calls.raygen.dev`)",
                "traefik.http.routers.phone-calls-deny.entrypoints=web",
                "traefik.http.routers.phone-calls-deny.priority=200",
                "traefik.http.routers.phone-calls-deny.service=noop@internal",
                "traefik.http.routers.phone-calls-deny.observability.accesslogs=false",
                "traefik.http.routers.phone-calls-deny.observability.tracing=false",
            ],
        },
        { provider: ctx.consulProvider, dependsOn: [speechNode, setup] },
    );
}
