import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ServiceContext } from "../../framework";

export const name = "phone-voice";
export const provides = ["phone-voice-setup"];
export const dependencies = ["huddle-phone-host"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    const host = ctx.commands.get("huddle-phone-host");
    if (!host) throw new Error("phone-voice requires the enrolled huddle-phone VM");
    const connection = {
        host: "192.168.0.233", user: "root", privateKey: ctx.sshPrivateKey,
        hostKey: new pulumi.Config("huddle-phone").require("sshHostKey"),
        dialErrorLimit: 30, perDialTimeout: 10,
    };
    const source = path.join(__dirname, "source");
    const hash = crypto.createHash("sha256");
    function visit(dir: string): void {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
            if (entry.isSymbolicLink() || ["state", "models", ".venv", ".env", "home-assistant.json", "__pycache__"].includes(entry.name)) {
                throw new Error(`Private or generated file in phone-voice source: ${entry.name}`);
            }
            const file = path.join(dir, entry.name);
            if (entry.isDirectory()) visit(file);
            else hash.update(path.relative(source, file)).update("\0").update(fs.readFileSync(file)).update("\0");
        }
    }
    visit(source);
    const revision = hash.digest("hex");
    const release = `/opt/phone-voice/releases/${revision}`;
    const stageScript = `install -d -m 0755 '${release}'`;
    const stage = new command.remote.Command("phone-voice-stage", {
        connection, create: stageScript, update: stageScript, addPreviousOutputInEnv: false,
    }, { dependsOn: [host] });
    const upload = new command.remote.CopyToRemote("phone-voice-source", {
        connection, source: new pulumi.asset.FileArchive(source), remotePath: release,
        triggers: [revision],
    }, { dependsOn: [stage] });
    const read = (file: string) => fs.readFileSync(path.join(__dirname, file), "utf8");
    const payload = pulumi.all({
        release: `${release}/source`,
        homeAssistant: config.requireSecret("homeAssistantConfig"),
        tunnelKey: config.require("tunnelPublicKey"),
        codexService: read("codex-phone-bridge.service"),
        assistService: read("home-assistant-phone.service"),
        operatorSecret: new pulumi.Config("huddle-phone").getSecret("operatorSecret") ?? "",
        operatorOwnerId: new pulumi.Config("huddle-phone").get("operatorOwnerId") ?? "",
    }).apply(value => JSON.stringify(value));
    const provision = `python3 -c '${read("provision.py").replace(/'/g, "'\\''")}'`;
    const setup = new command.remote.Command("phone-voice-setup", {
        connection, create: provision, update: provision, stdin: payload,
        logging: "none", triggers: [revision, payload], addPreviousOutputInEnv: false,
    }, { dependsOn: [upload], additionalSecretOutputs: ["stdout", "stderr"],
        customTimeouts: { create: "20m", update: "20m" } });
    ctx.commands.set("phone-voice-setup", setup);
    const dhcpScript = `python3 -c '${read("configure-dhcp.py").replace(/'/g, "'\\''")}'`;
    new command.remote.Command("phone-dhcp", {
        connection, create: dhcpScript, update: dhcpScript,
        stdin: JSON.stringify({ enabled: config.getBoolean("dhcpEnabled") ?? false }),
        addPreviousOutputInEnv: false,
    }, { dependsOn: [host] });
}
