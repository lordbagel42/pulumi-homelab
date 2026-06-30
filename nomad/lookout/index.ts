import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as nomad from "@pulumi/nomad";
import { NOMAD_IP } from "../../lxcs/hashistack";
import { ip } from "../../framework";
import { readSecret, managedSecret } from "../../infisical";
import type { ServiceContext } from "../../framework";

export const name = "lookout";
export const provides = ["lookout-deploy"];
export const dependencies = ["nomad-setup", "traefik-setup"];

const NOMAD_CLIENT_IP = ip(230);

export function register(ctx: ServiceContext): void {
    // ── Secrets (all from Infisical /lookout) ──────────────────────────────────
    const lookoutCfg = { ...ctx.infisicalConfig, secretPath: "/homelab/lookout" };
    const pgPassword    = managedSecret("LOOKOUT_PG_PASSWORD",    lookoutCfg);
    const adminPassword = managedSecret("LOOKOUT_ADMIN_PASSWORD", lookoutCfg);
    const r2AccountId   = readSecret("R2_ACCOUNT_ID",             lookoutCfg);
    const r2KeyId       = readSecret("R2_ACCESS_KEY_ID",          lookoutCfg);
    const r2Secret      = readSecret("R2_SECRET_ACCESS_KEY",      lookoutCfg);
    const r2Domain      = readSecret("R2_PUBLIC_DOMAIN",          lookoutCfg);

    // ── Prep: create host volume dir + register it in nomad-client config ─────
    const nomadSetupDep = ctx.commands.get("nomad-setup");
    const traefikDep    = ctx.commands.get("traefik-setup");
    const prepareDeps   = [nomadSetupDep, traefikDep].filter(
        (r): r is pulumi.Resource => r !== undefined
    );

    const prepCmd = new command.local.Command("lookout-prep", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${NOMAD_CLIENT_IP} "
  mkdir -p /opt/nomad/volumes/lookout_pgdata
  if ! grep -q 'lookout_pgdata' /etc/nomad.d/nomad.hcl 2>/dev/null; then
    sed -i '/^}.*$/!b; /node_class/!b; a\\\\\\n  host_volume \\"lookout_pgdata\\" {\\n    path      = \\"/opt/nomad/volumes/lookout_pgdata\\"\\n    read_only = false\\n  }' /etc/nomad.d/nomad.hcl
    systemctl restart nomad
    sleep 5
  fi
"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: { _SSH_KEY: ctx.sshPrivateKey },
    }, { dependsOn: prepareDeps });

    // ── Nomad provider (homelab cluster) ───────────────────────────────────────
    const nomadProvider = new nomad.Provider("homelab-nomad", {
        address: `http://${NOMAD_IP}:4646`,
    }, { dependsOn: [prepCmd] });

    const jobOpts: pulumi.ResourceOptions = {
        provider: nomadProvider,
        dependsOn: [prepCmd],
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

    new nomad.Job("lookout-updater", {
        jobspec: fs.readFileSync(updaterPath, "utf-8"),
    }, jobOpts);

    ctx.commands.set("lookout-deploy", prepCmd);
}
