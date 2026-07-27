import * as path from "path";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword, managedSecret, readSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";
import { CONSUL_IP_CONST } from "../hashistack";

export const name = "authentik";
export const provides = ["authentik-setup"];
export const dependencies: string[] = ["consul-setup"];

export function register(ctx: ServiceContext) {
    const VMID = 220;
    const IP = ip(VMID);

    const authentik = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: VMID,
        ip: IP,
        cpu: 2,
        memory: 4096,
        disk: 20,
        nesting: true,
        privileged: true,
        tags: ["auth", "system"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),
        reverseProxy: {
            domain: "auth.bagelindustries.com",
            port: 9000,
            entrypoint: "web",
            extraTags: [
                // Defines the `authentik@consulcatalog` middleware that every
                // `protected: true` service references — without it those routers
                // fail to build and their hosts return 500.
                `traefik.http.middlewares.authentik.forwardauth.address=http://${IP}:9000/outpost.goauthentik.io/auth/traefik`,
                "traefik.http.middlewares.authentik.forwardauth.trustForwardHeader=true",
                "traefik.http.middlewares.authentik.forwardauth.authResponseHeaders=" + [
                    "X-authentik-username",
                    "X-authentik-groups",
                    "X-authentik-entitlements",
                    "X-authentik-email",
                    "X-authentik-name",
                    "X-authentik-uid",
                    "X-authentik-jwt",
                    "X-authentik-meta-jwks",
                    "X-authentik-meta-outpost",
                    "X-authentik-meta-provider",
                    "X-authentik-meta-app",
                    "X-authentik-meta-version",
                ].join(","),
                // The embedded outpost's callback must resolve on *every*
                // protected host, not just auth.bagelindustries.com, so it gets
                // its own high-priority path router.
                "traefik.http.routers.authentik-outpost.rule=PathPrefix(`/outpost.goauthentik.io/`)",
                "traefik.http.routers.authentik-outpost.entrypoints=web",
                "traefik.http.routers.authentik-outpost.priority=100",
                "traefik.http.routers.authentik-outpost.service=authentik",
            ],
        },
        consulProvider: ctx.consulProvider,
    }, { provider: ctx.provider });

    // Only non-secret values may go through extraVars — see the note in
    // playbook.yml next to the .env task.
    const provision = ansibleProvision("authentik-provision", {
        host: IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            consul_ip: CONSUL_IP_CONST,
        },
        dependsOn: [authentik],
    });

    const pgPass           = managedSecret("authentik-pg-pass", ctx.infisicalConfig);
    const secretKey        = managedSecret("authentik-secret-key", ctx.infisicalConfig);
    const bootstrapPass    = readSecret("ADMIN_USER_PASS", { ...ctx.infisicalConfig, secretPath: "/" });
    const bootstrapToken   = managedSecret("authentik-bootstrap-token", ctx.infisicalConfig);
    const bootstrapEmail   = managedSecret("authentik-bootstrap-email", ctx.infisicalConfig);

    // Writes .env over SSH so the secret Outputs resolve properly, then brings
    // the stack up. Values are piped through stdin from the command's
    // environment, so they never appear in argv on either end.
    const credentials = new command.local.Command("authentik-credentials", {
        interpreter: ["/bin/bash", "-c"],
        create: `
set -euo pipefail
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
trap 'rm -f "$key"' EXIT
SSH="ssh -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10"
HOST=root@${IP}

{
  printf 'PG_PASS=%s\n'                     "$_PG_PASS"
  printf 'SECRET_KEY=%s\n'                  "$_SECRET_KEY"
  printf 'AUTHENTIK_BOOTSTRAP_PASSWORD=%s\n' "$_BOOTSTRAP_PASSWORD"
  printf 'AUTHENTIK_BOOTSTRAP_TOKEN=%s\n'    "$_BOOTSTRAP_TOKEN"
  printf 'AUTHENTIK_BOOTSTRAP_EMAIL=%s\n'    "$_BOOTSTRAP_EMAIL"
} | $SSH "$HOST" "install -d -m 0755 /opt/authentik && umask 077 && cat > /opt/authentik/.env"

$SSH "$HOST" "cd /opt/authentik && docker compose up -d"
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _PG_PASS: pgPass,
            _SECRET_KEY: secretKey,
            _BOOTSTRAP_PASSWORD: bootstrapPass,
            _BOOTSTRAP_TOKEN: bootstrapToken,
            _BOOTSTRAP_EMAIL: bootstrapEmail,
        },
        // Re-run when the compose file is rewritten or a secret rotates.
        triggers: [provision.id, pgPass, secretKey, bootstrapPass, bootstrapToken, bootstrapEmail],
    }, { dependsOn: [provision] });

    // Dependents wait for Authentik to actually be running, not merely installed.
    ctx.commands.set("authentik-setup", credentials);
}
