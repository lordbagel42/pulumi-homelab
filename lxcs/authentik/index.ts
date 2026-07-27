import * as path from "path";
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

    const provision = ansibleProvision("authentik-provision", {
        host: IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            consul_ip: CONSUL_IP_CONST,
            pg_pass: managedSecret("authentik-pg-pass", ctx.infisicalConfig),
            secret_key: managedSecret("authentik-secret-key", ctx.infisicalConfig),
            bootstrap_password: readSecret("ADMIN_USER_PASS", { ...ctx.infisicalConfig, secretPath: "/" }),
            bootstrap_token: managedSecret("authentik-bootstrap-token", ctx.infisicalConfig),
            bootstrap_email: managedSecret("authentik-bootstrap-email", ctx.infisicalConfig),
        },
        dependsOn: [authentik],
    });

    ctx.commands.set("authentik-setup", provision);
}
