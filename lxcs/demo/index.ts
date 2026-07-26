import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as nomad from "@pulumi/nomad";
import { NOMAD_IP } from "../hashistack";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "demo";
export const provides = ["demo-deploy"];
// "nomad-client-setup" was listed here but nothing in the repo provides it — the
// nomad client at .230 is a static VM outside Pulumi (see architecture.ts).
// ctx.commands.get() returned undefined and the filter below silently dropped it,
// so the declared dependency was never real. Depend on what actually exists.
export const dependencies = ["authentik-setup", "nomad-setup"];

export function register(ctx: ServiceContext): void {
    const dependsOn = [
        ctx.commands.get("authentik-setup"),
        ctx.commands.get("nomad-setup"),
    ].filter((r): r is pulumi.Resource => r !== undefined);

    // Was a create-only command.local.Command running `nomad job run` over SSH.
    // That submits the job exactly once: when the nomad-server rebuild wiped the
    // raft state holding every registration, Pulumi saw an unchanged Command and
    // had nothing to do, so demo stayed unregistered and demo.bagelindustries.com
    // stayed dark. A typed Job is state-tracked, so refresh sees it missing and up
    // resubmits it — the same reason the oracle jobs came back and this did not.
    const nomadProvider = new nomad.Provider("demo-nomad", {
        address: `http://${NOMAD_IP}:4646`,
    }, { dependsOn });

    const demoJob = new nomad.Job("demo", {
        jobspec: fs.readFileSync(path.join(__dirname, "demo.nomad.hcl"), "utf-8"),
    }, { provider: nomadProvider, dependsOn });

    ctx.commands.set("demo-deploy", demoJob);
}
