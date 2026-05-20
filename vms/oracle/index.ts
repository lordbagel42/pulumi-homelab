import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../../lxcs/hashistack";
import { TRAEFIK_VERSION } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "oracle";
export const provides = ["oracle-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): void {
    if (!ctx.oraclePublicIp || !ctx.oraclePrivateKey || !ctx.oracleUser || !ctx.oracleCfTunnelToken || !ctx.oracleNbIp) {
        throw new Error("oracle module requires oraclePublicIp, oraclePrivateKey, oracleUser, oracleCfTunnelToken, oracleNbIp in ServiceContext");
    }

    const scriptPath = path.join(__dirname, "oracle-setup.sh");

    // Static constants embedded directly; secrets passed via environment vars.
    const setupCmd = new command.local.Command("oracle-setup", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\\n' "$_ORACLE_PRIVATE_KEY" > "$key"
echo "Waiting for SSH on $_ORACLE_PUBLIC_IP..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes "$_ORACLE_USER@$_ORACLE_PUBLIC_IP" echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no "$_ORACLE_USER@$_ORACLE_PUBLIC_IP" \\
  "CONSUL_VERSION=${CONSUL_VERSION} CONSUL_IP=${CONSUL_IP_CONST} TRAEFIK_VERSION=${TRAEFIK_VERSION} ORACLE_NB_IP=$_ORACLE_NB_IP ORACLE_CF_TUNNEL_TOKEN=$_ORACLE_CF_TOKEN sudo -E bash -s" < "${scriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _ORACLE_PRIVATE_KEY: pulumi.output(ctx.oraclePrivateKey),
            _ORACLE_PUBLIC_IP: pulumi.output(ctx.oraclePublicIp),
            _ORACLE_USER: pulumi.output(ctx.oracleUser),
            _ORACLE_NB_IP: pulumi.output(ctx.oracleNbIp),
            _ORACLE_CF_TOKEN: pulumi.output(ctx.oracleCfTunnelToken),
        },
    });

    ctx.commands.set("oracle-setup", setupCmd);
}
