import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";
import { InfisicalConfig, lxcPassword, managedSecret } from "../../infisical";
import { CONSUL_VERSION, CONSUL_IP_CONST } from "../hashistack";
import { ip, GATEWAY } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "authentik";
export const provides = ["authentik-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): command.local.Command {
    return createAuthentik({
        provider: ctx.provider,
        infisicalConfig: ctx.infisicalConfig,
        sshKey: ctx.sshKey,
        sshPrivateKey: ctx.sshPrivateKey,
        pgPass: managedSecret("authentik-pg-pass", ctx.infisicalConfig),
        secretKey: managedSecret("authentik-secret-key", ctx.infisicalConfig),
        bootstrapPassword: managedSecret("ADMIN_USER_PASS", ctx.infisicalConfig),
        bootstrapToken: managedSecret("authentik-bootstrap-token", ctx.infisicalConfig),
    }, cmd => ctx.commands.set("authentik-setup", cmd));
}

const AUTHENTIK_VMID = 220;
export const AUTHENTIK_IP = ip(AUTHENTIK_VMID);

export interface AuthentikArgs {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    pgPass: pulumi.Output<string>;
    secretKey: pulumi.Output<string>;
    bootstrapPassword: pulumi.Output<string>;
    bootstrapToken: pulumi.Output<string>;
}

export function createAuthentik({
    provider,
    infisicalConfig,
    sshKey,
    sshPrivateKey,
    pgPass,
    secretKey,
    bootstrapPassword,
    bootstrapToken,
}: AuthentikArgs, onSetup?: (cmd: command.local.Command) => void): command.local.Command {
    const scriptPath = path.join(__dirname, "authentik-setup.sh");

    const container = new proxmox.ContainerLegacy("authentik", {
        nodeName: "optiplex",
        vmId: AUTHENTIK_VMID,
        cpu: { cores: 2 },
        memory: { dedicated: 4096 },
        disk: { datastoreId: "local-lvm", size: 20 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "authentik",
            ipConfigs: [{ ipv4: { address: `${AUTHENTIK_IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("authentik", infisicalConfig), keys: [sshKey] },
        },
        features: { nesting: true },
        unprivileged: false,
        tags: ["auth", "system"],
        startOnBoot: true,
        started: true,
    }, { provider });

    const setupCmd = new command.local.Command("authentik-setup", {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
echo "Waiting for SSH on ${AUTHENTIK_IP}..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${AUTHENTIK_IP} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no root@${AUTHENTIK_IP} \\
  "CONSUL_VERSION=${CONSUL_VERSION} CONSUL_IP=${CONSUL_IP_CONST} AUTHENTIK_IP=${AUTHENTIK_IP} PG_PASS=$_PG_PASS SECRET_KEY=$_SECRET_KEY BOOTSTRAP_PASSWORD=$_BOOTSTRAP_PASSWORD BOOTSTRAP_TOKEN=$_BOOTSTRAP_TOKEN bash -s" \\
  < "${scriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _SSH_KEY: sshPrivateKey,
            _PG_PASS: pgPass,
            _SECRET_KEY: secretKey,
            _BOOTSTRAP_PASSWORD: bootstrapPassword,
            _BOOTSTRAP_TOKEN: bootstrapToken,
        },
    }, { dependsOn: [container] });
    onSetup?.(setupCmd);
    return setupCmd;
}
