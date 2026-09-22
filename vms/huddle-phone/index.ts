import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ip, ServiceContext } from "../../framework";
import { ProxmoxMachine } from "../../framework/proxmox-machine";

export const name = "huddle-phone";
export const provides = ["huddle-phone-deploy", "huddle-phone-host"];
export const dependencies: string[] = [];

function projectHash(directory: string): string {
    const hash = crypto.createHash("sha256");
    function visit(current: string): void {
        for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
            const file = path.join(current, entry.name);
            if (entry.isSymbolicLink()) throw new Error(`Symlinks are not allowed in the deployment snapshot: ${file}`);
            if ([".env", "generated", "state", "node_modules", ".venv", "__pycache__"].includes(entry.name)) {
                throw new Error(`Private or generated file in deployment snapshot: ${file}`);
            }
            if (entry.isDirectory()) visit(file);
            else {
                hash.update(path.relative(directory, file));
                hash.update("\0");
                hash.update(fs.readFileSync(file));
                hash.update("\0");
            }
        }
    }
    visit(directory);
    return hash.digest("hex");
}

function dotenv(value: string): string {
    if (/[\r\n'\0]/.test(value)) throw new Error("Huddle environment values must be single-line and contain no single quotes");
    return `'${value}'`;
}

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;

    // Node-local cloud image/snippet supplied by index.ts are on optiplex.
    const vmId = 233;
    const address = ip(vmId);
    const machine = new ProxmoxMachine(name, {
        type: "vm", nodeName: "optiplex", vmId, ip: address,
        cpu: 4, cpuType: "x86-64-v3", memory: 4096, disk: 32,
        importFrom: ctx.debianCloudImageId,
        userDataFileId: ctx.cloudInitSnippetId,
        sshKeys: [ctx.sshKey], password: ctx.vmPassword,
        tags: ["phone", "slack"],
    }, { provider: ctx.provider, protect: true });
    ctx.commands.set("huddle-phone-deploy", machine);

    // Enrol the new SSH host through Proxmox's guest agent before sending
    // account credentials. The first deployment creates only the VM.
    const hostKey = config.get("sshHostKey");
    if (!hostKey) {
        pulumi.log.info(`Created/enrolling ${name} at ${address}; set huddle-phone:sshHostKey from the Proxmox guest agent to provision it.`, machine);
        return;
    }

    const connection = {
        host: address, user: "root", privateKey: ctx.sshPrivateKey, hostKey,
        dialErrorLimit: 60, perDialTimeout: 10,
    };
    const script = (file: string): string => fs.readFileSync(path.join(__dirname, file), "utf8");
    // Pass the script as an argument: interactive child commands must never
    // consume the remaining provisioning script from Bash's standard input.
    const bash = (body: string): string => `bash -e -c '${body.replace(/'/g, "'\\''")}'`;
    const setup = bash(script("prepare-host.sh"));
    const host = new command.remote.Command("huddle-phone-host", {
        connection, create: setup, update: setup, addPreviousOutputInEnv: false,
    }, { dependsOn: [machine], customTimeouts: { create: "20m", update: "20m" } });
    ctx.commands.set("huddle-phone-host", host);

    const source = path.join(__dirname, "project");
    const revision = projectHash(source);
    const release = `/opt/huddle-phone/releases/${revision}`;
    const stageScript = `install -d -m 0700 '${release}'`;
    const stage = new command.remote.Command("huddle-phone-stage", {
        connection, create: stageScript, update: stageScript, addPreviousOutputInEnv: false,
    }, { dependsOn: [host] });
    // Copy the project directory inside the existing release directory. Releases
    // are content-addressed so removed source files cannot survive an update.
    const upload = new command.remote.CopyToRemote("huddle-phone-source", {
        connection, source: new pulumi.asset.FileArchive(source), remotePath: release,
        triggers: [revision],
    }, { dependsOn: [stage] });

    // Keep a canonical environment file so identical configuration never
    // triggers a deployment because its property order changed.
    const environment = pulumi.all({
        SLACK_WORKSPACE: config.require("workspace"),
        SLACK_TEAM_ID: config.require("teamId"),
        SLACK_ENTERPRISE_ID: config.get("enterpriseId") ?? "",
        SLACK_CHANNEL_ID: config.require("channelId"),
        SLACK_CLIENT_TOKEN: config.requireSecret("clientToken"),
        SLACK_COOKIE: config.requireSecret("cookie"),
        AMI_SECRET: config.requireSecret("amiSecret"),
        OPERATOR_SECRET: config.getSecret("operatorSecret") ?? "",
        AMI_USER: "huddle-phone", AMI_HOST: "127.0.0.1", AMI_PORT: "5038",
        PHONE_CHANNEL: config.get("phoneChannel") ?? "SCCP/6738",
        DRY_RUN: String(config.getBoolean("dryRun") ?? true),
        INCOMING_INVITES: String(config.getBoolean("incomingInvites") ?? true),
        CHIME_REGION: "us-east-2", AUDIO_PORT: "9094", HTTP_PORT: "8099",
        RING_SECONDS: "30", MAX_CALL_SECONDS: "3600", MAX_HUDDLE_AGE_SECONDS: "120", POLL_SECONDS: "5",
    }).apply(values => Object.entries(values).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
        .map(([key, value]) => `${key}=${dotenv(value)}`).join("\n") + "\n");
    const writeEnvironment = `set -eu
umask 077
test -f '${release}/project/huddle-phone/configure.py'
env_file=$(mktemp '${release}/project/huddle-phone/.env.XXXXXX')
trap 'rm -f "$env_file"' EXIT
cat > "$env_file"
chmod 0600 "$env_file"
mv "$env_file" '${release}/project/huddle-phone/.env'
`;
    const credentials = new command.remote.Command("huddle-phone-environment", {
        connection, create: writeEnvironment, update: writeEnvironment,
        stdin: environment, logging: "none", addPreviousOutputInEnv: false,
    }, { dependsOn: [upload], additionalSecretOutputs: ["stdout", "stderr"] });

    const deployScript = bash(`export HUDDLE_RELEASE='${release}/project'\nexport HUDDLE_IP='${address}'\n${script("deploy.sh")}`);
    const deploy = new command.remote.Command("huddle-phone-deploy", {
        connection, create: deployScript, update: deployScript,
        triggers: [revision, environment], addPreviousOutputInEnv: false,
    }, { dependsOn: [credentials], customTimeouts: { create: "40m", update: "40m" } });
    ctx.commands.set("huddle-phone-deploy", deploy);
}
