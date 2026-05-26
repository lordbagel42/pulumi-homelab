import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";

export interface GrafanaConfig {
    logsUrl: pulumi.Output<string>;
    logsId: pulumi.Output<string>;
    metricsUrl: pulumi.Output<string>;
    metricsId: pulumi.Output<string>;
    apiKey: pulumi.Output<string>;
    scrapeInterval: pulumi.Output<string>;
}

export function installAlloy(
    name: string,
    host: string,
    nodeLabel: string,
    grafana: GrafanaConfig,
    sshPrivateKey: pulumi.Output<string>,
    dependsOn: pulumi.Resource[],
    sshUser: string = "root",
): command.local.Command {
    const scriptPath = path.join(__dirname, "alloy-setup.sh");
    const scriptHash = crypto.createHash("sha256").update(fs.readFileSync(scriptPath)).digest("hex");

    return new command.local.Command(`${name}-alloy`, {
        triggers: [scriptHash],
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
scp -i "$key" -o StrictHostKeyChecking=no "${scriptPath}" ${sshUser}@${host}:/tmp/alloy-setup.sh
ssh -i "$key" -o StrictHostKeyChecking=no ${sshUser}@${host} \
  "ALLOY_NODE_LABEL=${nodeLabel} GCLOUD_HOSTED_LOGS_URL=$_LOGS_URL GCLOUD_HOSTED_LOGS_ID=$_LOGS_ID GCLOUD_HOSTED_METRICS_URL=$_METRICS_URL GCLOUD_HOSTED_METRICS_ID=$_METRICS_ID GCLOUD_RW_API_KEY=$_API_KEY GCLOUD_SCRAPE_INTERVAL=$_SCRAPE_INTERVAL bash /tmp/alloy-setup.sh"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _SSH_KEY: sshPrivateKey,
            _LOGS_URL: grafana.logsUrl,
            _LOGS_ID: grafana.logsId,
            _METRICS_URL: grafana.metricsUrl,
            _METRICS_ID: grafana.metricsId,
            _API_KEY: grafana.apiKey,
            _SCRAPE_INTERVAL: grafana.scrapeInterval,
        },
    }, { dependsOn });
}
