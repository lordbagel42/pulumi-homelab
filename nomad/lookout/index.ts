import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as nomad from "@pulumi/nomad";
import { NOMAD_IP } from "../../lxcs/hashistack";
import { readSecret, managedSecret } from "../../infisical";
import type { ServiceContext } from "../../framework";

export const name = "lookout";
export const provides = ["lookout-deploy"];
export const dependencies = ["nomad-setup", "traefik-setup", "nomad-client-setup"];

export function register(ctx: ServiceContext): void {
    // ── Secrets (all from Infisical /lookout) ──────────────────────────────────
    const lookoutCfg = { ...ctx.infisicalConfig, secretPath: "/homelab/lookout" };
    const pgPassword    = managedSecret("LOOKOUT_PG_PASSWORD",    lookoutCfg);
    const adminPassword = managedSecret("LOOKOUT_ADMIN_PASSWORD", lookoutCfg);
    const r2AccountId   = readSecret("R2_ACCOUNT_ID",             lookoutCfg);
    const r2KeyId       = readSecret("R2_ACCESS_KEY_ID",          lookoutCfg);
    const r2Secret      = readSecret("R2_SECRET_ACCESS_KEY",      lookoutCfg);
    const r2Domain      = readSecret("R2_PUBLIC_DOMAIN",          lookoutCfg);

    // The lookout_pgdata host volume is declared by the nomad-client module, so
    // all that is left here is to wait for the cluster to be ready.
    const clusterDeps = ["nomad-setup", "traefik-setup", "nomad-client-setup"]
        .map((c) => ctx.commands.get(c))
        .filter((r): r is pulumi.Resource => r !== undefined);

    // ── Nomad provider (homelab cluster) ───────────────────────────────────────
    const nomadProvider = new nomad.Provider("homelab-nomad", {
        address: `http://${NOMAD_IP}:4646`,
    }, { dependsOn: clusterDeps });

    const jobOpts: pulumi.ResourceOptions = {
        provider: nomadProvider,
        dependsOn: clusterDeps,
    };

    // ── Main service job ───────────────────────────────────────────────────────
    const jobPath = path.join(__dirname, "lookout.nomad.hcl");

    new nomad.Job("lookout", {
        jobspec: pulumi.all([pgPassword, adminPassword, r2AccountId, r2KeyId, r2Secret, r2Domain]).apply(
            ([pgPass, adminPass, accountId, keyId, secret, domain]) =>
                fs.readFileSync(jobPath, "utf-8")
                    .replace(/__PG_PASSWORD__/g,        pgPass)
                    .replace("__ADMIN_USERNAME__",       "admin")
                    .replace("__ADMIN_PASSWORD__",       adminPass)
                    .replace("__R2_ACCOUNT_ID__",        accountId)
                    .replace("__R2_ACCESS_KEY_ID__",     keyId)
                    .replace("__R2_SECRET_ACCESS_KEY__", secret)
                    .replace("__R2_PUBLIC_DOMAIN__",     domain)
        ),
    }, jobOpts);

    // ── Periodic updater job ───────────────────────────────────────────────────
    const updaterPath = path.join(__dirname, "lookout-update.nomad.hcl");

    const updater = new nomad.Job("lookout-updater", {
        jobspec: fs.readFileSync(updaterPath, "utf-8"),
    }, jobOpts);

    ctx.commands.set("lookout-deploy", updater);
}
