import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as nomad from "@pulumi/nomad";
import { CONSUL_VERSION, CONSUL_IP_CONST, NOMAD_IP } from "../../lxcs/hashistack";
import { NOMAD_VERSION, TRAEFIK_VERSION } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "oracle";
export const provides = ["oracle-setup", "oracle-jobs"];
export const dependencies = ["nomad-setup"];

export function register(ctx: ServiceContext): void {
    if (!ctx.oraclePublicIp || !ctx.oraclePrivateKey || !ctx.oracleUser || !ctx.oracleCfTunnelToken || !ctx.oracleNbIp) {
        throw new Error("oracle module requires oraclePublicIp, oraclePrivateKey, oracleUser, oracleCfTunnelToken, oracleNbIp in ServiceContext");
    }

    const scriptPath = path.join(__dirname, "oracle-setup.sh");
    const nomadSetupDep = ctx.commands.get("nomad-setup");

    const setupCmd = new command.local.Command("oracle-setup", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_ORACLE_PRIVATE_KEY" > "$key"
echo "Waiting for SSH on $_ORACLE_PUBLIC_IP..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes "$_ORACLE_USER@$_ORACLE_PUBLIC_IP" echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no "$_ORACLE_USER@$_ORACLE_PUBLIC_IP" \\
  "CONSUL_VERSION=${CONSUL_VERSION} CONSUL_IP=${CONSUL_IP_CONST} NOMAD_VERSION=${NOMAD_VERSION} ORACLE_NB_IP=$_ORACLE_NB_IP NOMAD_SERVER_IP=${NOMAD_IP} sudo -E bash -s" \\
  < "${scriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _ORACLE_PRIVATE_KEY: pulumi.output(ctx.oraclePrivateKey),
            _ORACLE_PUBLIC_IP: pulumi.output(ctx.oraclePublicIp),
            _ORACLE_USER: pulumi.output(ctx.oracleUser),
            _ORACLE_NB_IP: pulumi.output(ctx.oracleNbIp),
        },
    }, { dependsOn: nomadSetupDep ? [nomadSetupDep] : [] });
    ctx.commands.set("oracle-setup", setupCmd);

    // ── Nomad jobs via typed provider (no SSH/shell) ───────────────────────────
    const nomadProvider = new nomad.Provider("oracle-nomad", {
        address: `http://${NOMAD_IP}:4646`,
    });
    const jobOpts: pulumi.ResourceOptions = { provider: nomadProvider, dependsOn: [setupCmd] };

    const traefikJob = new nomad.Job("oracle-traefik", {
        jobspec: fs.readFileSync(path.join(__dirname, "traefik.nomad.hcl"), "utf-8")
            .replace("__TRAEFIK_VERSION__", TRAEFIK_VERSION),
    }, jobOpts);
    ctx.commands.set("oracle-jobs", traefikJob);

    const pelicanAppKey    = ctx.oraclePelicanAppKey    ?? pulumi.output("");
    const pelicanDbPass    = ctx.oraclePelicanDbPass    ?? pulumi.output("");
    const pelicanDbRootPass = ctx.oraclePelicanDbRootPass ?? pulumi.output("");

    new nomad.Job("oracle-pelican", {
        jobspec: pulumi.all([pelicanAppKey, pelicanDbPass, pelicanDbRootPass]).apply(
            ([appKey, dbPass, dbRootPass]) =>
                fs.readFileSync(path.join(__dirname, "pelican.nomad.hcl"), "utf-8")
                    .replace("__APP_KEY__", appKey)
                    .replace(/__DB_PASSWORD__/g, dbPass)
                    .replace("__DB_ROOT_PASSWORD__", dbRootPass)
        ),
    }, jobOpts);

    new nomad.Job("oracle-cloudflared", {
        jobspec: pulumi.output(ctx.oracleCfTunnelToken).apply(token =>
            fs.readFileSync(path.join(__dirname, "cloudflared.nomad.hcl"), "utf-8")
                .replace("__CF_TOKEN__", token)
        ),
    }, jobOpts);

    // ── Alloy observability on oracle VM ───────────────────────────────────────
    if (ctx.grafana) {
        const g = ctx.grafana;
        const alloyScriptPath = path.join(__dirname, "../../utils/alloy-setup.sh");
        const alloyScriptHash = crypto.createHash("sha256").update(fs.readFileSync(alloyScriptPath)).digest("hex");

        new command.local.Command("oracle-alloy", {
            triggers: [alloyScriptHash],
            create: pulumi.interpolate`
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
scp -i "$key" -o StrictHostKeyChecking=no "${alloyScriptPath}" "$_USER@$_HOST":/tmp/alloy-setup.sh
ssh -i "$key" -o StrictHostKeyChecking=no "$_USER@$_HOST" \
  "ALLOY_NODE_LABEL=oracle GCLOUD_HOSTED_LOGS_URL=${g.logsUrl} GCLOUD_HOSTED_LOGS_ID=${g.logsId} GCLOUD_HOSTED_METRICS_URL=${g.metricsUrl} GCLOUD_HOSTED_METRICS_ID=${g.metricsId} GCLOUD_RW_API_KEY=${g.apiKey} GCLOUD_SCRAPE_INTERVAL=${g.scrapeInterval} sudo -E bash /tmp/alloy-setup.sh"
rc=$?
rm -f "$key"
exit $rc
            `.apply(s => s.trim()),
            environment: {
                _SSH_KEY: pulumi.output(ctx.oraclePrivateKey),
                _HOST: pulumi.output(ctx.oraclePublicIp),
                _USER: pulumi.output(ctx.oracleUser),
            },
        }, { dependsOn: [setupCmd] });
    }
}
