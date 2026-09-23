import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as nomad from "@pulumi/nomad";
import { NOMAD_CLIENT_IP } from "../../lxcs/hashistack";
import type { ServiceContext } from "../../framework";

export const name = "ai-proxy";
export const provides = ["ai-proxy-deploy"];
export const dependencies = ["nomad-setup", "traefik-setup", "nomad-client-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    if (!ctx.nomadProvider) throw new Error("ai-proxy requires the homelab Nomad provider");

    const clusterDeps = dependencies
        .map((capability) => ctx.commands.get(capability))
        .filter((resource): resource is pulumi.Resource => resource !== undefined);
    const connection = {
        host: NOMAD_CLIENT_IP,
        user: "root",
        privateKey: ctx.sshPrivateKey,
        // Verify through the Proxmox guest agent when initially enrolling a host.
        hostKey: config.require("sshHostKey"),
    };
    const setup = fs.readFileSync(path.join(__dirname, "prepare-host.sh"), "utf-8");
    const storage = new command.remote.Command("ai-proxy-storage", {
        connection,
        create: setup,
        update: setup,
        addPreviousOutputInEnv: false,
    }, { dependsOn: clusterDeps });
    ctx.commands.set("ai-proxy-deploy", storage);

    // Storage can be provisioned before the application image and migration are ready.
    const image = config.get("image");
    if (!image) return;
    if (image.endsWith(":latest") || !image.includes(":")) {
        throw new Error("ai-proxy:image must be a pinned version or image digest");
    }
    const replicas = config.getNumber("replicas") ?? 1;
    if (replicas !== 0 && replicas !== 1) {
        throw new Error("ai-proxy supports zero or one replica to protect SQLite and OAuth refreshes");
    }
    const environment = config.requireSecret("environment");
    const writeEnvironment = `set -eu
umask 077
env_file=$(mktemp /etc/ai-proxy/.env.XXXXXX)
trap 'rm -f "$env_file"' EXIT
cat > "$env_file"
chown 1000:1000 "$env_file"
chmod 0400 "$env_file"
mv "$env_file" /etc/ai-proxy/ai-proxy.env
`;
    const secrets = new command.remote.Command("ai-proxy-environment", {
        connection,
        create: writeEnvironment,
        update: writeEnvironment,
        stdin: environment,
        logging: "none",
        addPreviousOutputInEnv: false,
    }, { dependsOn: [storage], additionalSecretOutputs: ["stdout", "stderr"] });

    // Nomad submissions may be API JSON after an out-of-band update. The
    // provider reads that source back into jobspec without changing its format
    // flag. Keep HCL as the source of truth, but use JSON consistently in state.
    const parsed = nomad.getJobParserOutput({
        hcl: fs.readFileSync(path.join(__dirname, "ai-proxy.nomad.hcl"), "utf-8"),
        variables: pulumi.jsonStringify({
            image,
            replicas,
            serving_enabled: config.getBoolean("servingEnabled") ?? false,
            // A changed environment file must restart the process that loaded it.
            environment_revision: environment.apply((value) =>
                crypto.createHash("sha256").update(value).digest("hex")),
        }),
    }, { provider: ctx.nomadProvider, dependsOn: clusterDeps });

    const job = new nomad.Job(name, {
        jobspec: parsed.json,
        json: true,
        detach: false,
        purgeOnDestroy: false,
    }, { provider: ctx.nomadProvider, dependsOn: [secrets, ...clusterDeps] });
    ctx.commands.set("ai-proxy-deploy", job);
}
