import * as path from "path";
import * as command from "@pulumi/command";
import { NOMAD_IP } from "../hashistack";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "demo";
export const provides = ["demo-deploy"];
export const dependencies = ["authentik-setup", "nomad-client-setup"];

export function register(ctx: ServiceContext): void {
    const demoJobPath = path.join(__dirname, "demo.nomad.hcl");
    const dependsOn = [
        ctx.commands.get("authentik-setup"),
        ctx.commands.get("nomad-client-setup"),
    ].filter((r): r is import("@pulumi/pulumi").Resource => r !== undefined);

    const demoCmd = new command.local.Command("demo-deploy", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${NOMAD_IP} "nomad job run -" < "${demoJobPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: { _SSH_KEY: ctx.sshPrivateKey },
    }, { dependsOn });

    ctx.commands.set("demo-deploy", demoCmd);
}
