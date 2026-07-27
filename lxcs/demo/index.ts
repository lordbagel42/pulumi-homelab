import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as nomad from "@pulumi/nomad";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "demo";
export const provides = ["demo-deploy"];
export const dependencies = ["authentik-setup", "nomad-client-setup"];

export function register(ctx: ServiceContext): void {
    if (!ctx.nomadProvider) {
        throw new Error("demo module requires nomadProvider in ServiceContext");
    }

    const dependsOn = [
        ctx.commands.get("authentik-setup"),
        ctx.commands.get("nomad-client-setup"),
    ].filter((r): r is pulumi.Resource => r !== undefined);

    // A typed Job rather than `nomad job run` over SSH: the job is already in
    // state under this name, so submitting it any other way would have Pulumi
    // deregister it again during the delete phase of the same update.
    const demoJob = new nomad.Job("demo", {
        jobspec: fs.readFileSync(path.join(__dirname, "demo.nomad.hcl"), "utf-8"),
    }, { provider: ctx.nomadProvider, dependsOn });

    ctx.commands.set("demo-deploy", demoJob);
}
