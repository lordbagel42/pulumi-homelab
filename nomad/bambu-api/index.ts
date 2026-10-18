import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as nomad from "@pulumi/nomad";
import { NOMAD_CLIENT_IP } from "../../lxcs/hashistack";
import type { ServiceContext } from "../../framework";
import { managedSecret } from "../../infisical";

export const name = "bambu-api";
export const provides = ["bambu-api-deploy"];
export const dependencies = ["nomad-setup", "traefik-setup", "nomad-client-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;
    if (!ctx.nomadProvider) throw new Error("bambu-api requires the homelab Nomad provider");

    const clusterDeps = dependencies
        .map((capability) => ctx.commands.get(capability))
        .filter((resource): resource is pulumi.Resource => resource !== undefined);
    const connection = {
        host: NOMAD_CLIENT_IP,
        user: "root",
        privateKey: ctx.sshPrivateKey,
        hostKey: config.require("sshHostKey"),
    };
    const setup = fs.readFileSync(path.join(__dirname, "prepare-host.sh"), "utf-8");
    const storage = new command.remote.Command("bambu-api-storage", {
        connection,
        create: setup,
        update: setup,
        addPreviousOutputInEnv: false,
    }, { dependsOn: clusterDeps });
    ctx.commands.set("bambu-api-deploy", storage);

    const image = config.get("image");
    if (!image) return;
    if (image.endsWith(":latest") || !image.includes(":")) {
        throw new Error("bambu-api:image must be a pinned version or image digest");
    }

    // Encrypts the stored Bambu sessions. Generated once and kept in Infisical:
    // a different key makes every stored session unreadable.
    const dataKey = managedSecret("BAMBU_API_DATA_KEY", { ...ctx.infisicalConfig, secretPath: "/bambu-api" });
    const environment = pulumi.secret(pulumi.interpolate`BAMBU_DATA_KEY=${dataKey}\n`);
    const writeEnvironment = `set -eu
umask 077
env_file=$(mktemp /etc/bambu-api/.env.XXXXXX)
trap 'rm -f "$env_file"' EXIT
cat > "$env_file"
chown 1000:1000 "$env_file"
chmod 0400 "$env_file"
mv "$env_file" /etc/bambu-api/bambu-api.env
`;
    const secrets = new command.remote.Command("bambu-api-environment", {
        connection,
        create: writeEnvironment,
        update: writeEnvironment,
        stdin: environment,
        logging: "none",
        addPreviousOutputInEnv: false,
    }, { dependsOn: [storage], additionalSecretOutputs: ["stdout", "stderr"] });

    // HCL is the source of truth; submit the parser's JSON in JSON mode, as
    // ai-proxy does, so refreshes of API-submitted jobs stay consistent.
    const parsed = nomad.getJobParserOutput({
        hcl: fs.readFileSync(path.join(__dirname, "bambu-api.nomad.hcl"), "utf-8"),
        variables: pulumi.jsonStringify({
            image,
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
    ctx.commands.set("bambu-api-deploy", job);
}
