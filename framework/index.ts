import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";
import { InfisicalConfig, lxcPassword } from "../infisical";
import { sshSetup } from "../utils/ssh";

// js-yaml is available as a transitive dep of @pulumi/pulumi
// eslint-disable-next-line @typescript-eslint/no-var-requires
const jsyaml = require("js-yaml") as { load(s: string): unknown };

// ── Shared constants & helpers ────────────────────────────────────────────────

export const CONSUL_VERSION = "1.20.1";
export const NOMAD_VERSION = "1.9.3";
export const TRAEFIK_VERSION = "3.2.0";
export const GATEWAY = "192.168.0.1";

/** Derive the LAN IP from a Proxmox VMID (all hosts live at 192.168.0.<vmid>). */
export function ip(vmid: number): string {
    return `192.168.0.${vmid}`;
}

// ── ServiceContext ─────────────────────────────────────────────────────────────

export interface ServiceContext {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    vmPassword: pulumi.Output<string>;
    debianCloudImageId: pulumi.Input<string>;
    cloudInitSnippetId: pulumi.Input<string>;
    cloudflaredTunnelToken: pulumi.Input<string>;
    /** PBS-specific extras (only set when PBS module needs them) */
    pbsBackupPassword?: pulumi.Input<string>;
    proxmoxEndpoint?: pulumi.Input<string>;
    proxmoxUsername?: pulumi.Input<string>;
    proxmoxPassword?: pulumi.Input<string>;
    /** Oracle VM extras */
    oraclePublicIp?: pulumi.Input<string>;
    oraclePrivateKey?: pulumi.Input<string>;
    oracleUser?: pulumi.Input<string>;
    oracleCfTunnelToken?: pulumi.Input<string>;
    oracleNbIp?: pulumi.Input<string>;
    /** Registry of named commands/resources, populated as services register. */
    commands: Map<string, pulumi.Resource>;
}

// ── Service module contract ───────────────────────────────────────────────────

export interface ServiceModule {
    /** Unique name for this service group (used for dependency resolution). */
    readonly name: string;
    /** Command/resource names this module registers in ctx.commands. */
    readonly provides?: string[];
    /** Command names from ctx.commands this module needs before running. */
    readonly dependencies?: string[];
    register(ctx: ServiceContext): void;
}

// ── YAML service schema ───────────────────────────────────────────────────────

interface YamlReverseProxy {
    domain: string;
    port: number;
    protected?: boolean;
    entrypoint?: string;
}

interface YamlSetup {
    script: string;
    env?: Record<string, string>;
}

interface YamlServiceConfig {
    name: string;
    vmid: number;
    type?: "lxc" | "vm";
    ip: string;
    gateway?: string;
    cpu?: number;
    memory?: number;
    disk?: number;
    tags?: string[];
    privileged?: boolean;
    nesting?: boolean;
    setup?: YamlSetup;
    reverseProxy?: YamlReverseProxy;
    dependsOn?: string[];
}

// ── YAML service registration ─────────────────────────────────────────────────

function registerYamlService(dir: string, cfg: YamlServiceConfig, ctx: ServiceContext): void {
    const gateway = cfg.gateway ?? GATEWAY;

    const container = new proxmox.ContainerLegacy(cfg.name, {
        nodeName: "optiplex",
        vmId: cfg.vmid,
        cpu: { cores: cfg.cpu ?? 1 },
        memory: { dedicated: cfg.memory ?? 512 },
        disk: { datastoreId: "local-lvm", size: cfg.disk ?? 8 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: cfg.name,
            ipConfigs: [{ ipv4: { address: `${cfg.ip}/24`, gateway } }],
            userAccount: { password: lxcPassword(cfg.name, ctx.infisicalConfig), keys: [ctx.sshKey] },
        },
        ...(cfg.nesting || cfg.privileged ? { features: { nesting: cfg.nesting ?? false } } : {}),
        unprivileged: !(cfg.privileged ?? false),
        tags: cfg.tags ?? [],
        startOnBoot: true,
        started: true,
    }, { provider: ctx.provider });

    if (!cfg.setup) return;

    const scriptPath = path.join(dir, cfg.setup.script);
    const dependsOn: pulumi.Resource[] = [container];
    for (const dep of cfg.dependsOn ?? []) {
        const res = ctx.commands.get(dep);
        if (res) dependsOn.push(res);
    }

    const setupCmd = sshSetup(
        `${cfg.name}-setup`,
        cfg.ip,
        scriptPath,
        cfg.setup.env ?? {},
        ctx.sshPrivateKey,
        dependsOn,
    );
    ctx.commands.set(`${cfg.name}-setup`, setupCmd);

    if (!cfg.reverseProxy) return;

    const rp = cfg.reverseProxy;
    const tags = [
        "traefik.enable=true",
        `traefik.http.routers.${cfg.name}.rule=Host(\`${rp.domain}\`)`,
        `traefik.http.routers.${cfg.name}.entrypoints=${rp.entrypoint ?? "web"}`,
        `traefik.http.services.${cfg.name}.loadbalancer.server.port=${rp.port}`,
        ...(rp.protected ? [`traefik.http.routers.${cfg.name}.middlewares=authentik@consulcatalog`] : []),
    ];
    const svcJson = JSON.stringify({ service: { name: cfg.name, port: rp.port, tags } }, null, 2);

    // Inject Consul registration after setup completes
    const regScript = `
key=$(mktemp)
chmod 600 "$key"
printf '%s\\n' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${cfg.ip} bash -s << 'REMOTE'
cat > /etc/consul.d/${cfg.name}-service.json << 'SVCEOF'
${svcJson}
SVCEOF
chown -R consul:consul /etc/consul.d/ 2>/dev/null || true
consul reload 2>/dev/null || true
REMOTE
rm -f "$key"
    `.trim();

    const regCmd = new command.local.Command(`${cfg.name}-consul-register`, {
        create: regScript,
        environment: { _SSH_KEY: ctx.sshPrivateKey },
    }, { dependsOn: [setupCmd] });
    ctx.commands.set(`${cfg.name}-consul-register`, regCmd);
}

// ── Discovery & topological sort ──────────────────────────────────────────────

interface ServiceDescriptor {
    name: string;
    dir: string;
    kind: "typescript" | "yaml";
    mod?: ServiceModule;
    yamlCfg?: YamlServiceConfig;
    provides: string[];
    dependencies: string[];
}

function buildDescriptors(baseDirs: string[]): ServiceDescriptor[] {
    const descriptors: ServiceDescriptor[] = [];

    for (const baseDir of baseDirs) {
        if (!fs.existsSync(baseDir)) continue;
        const entries = fs.readdirSync(baseDir, { withFileTypes: true });

        for (const entry of entries) {
            if (!entry.isDirectory()) continue;
            const dir = path.join(baseDir, entry.name);
            const tsPath = path.join(dir, "index.ts");
            const yamlPath = path.join(dir, "service.yaml");

            if (fs.existsSync(tsPath)) {
                // Require module to read its metadata (safe: no top-level resource creation)
                // eslint-disable-next-line @typescript-eslint/no-var-requires
                const mod = require(tsPath) as Partial<ServiceModule>;
                if (typeof mod.register !== "function") continue;
                const svcMod = mod as ServiceModule;
                descriptors.push({
                    name: svcMod.name,
                    dir,
                    kind: "typescript",
                    mod: svcMod,
                    provides: svcMod.provides ?? [],
                    dependencies: svcMod.dependencies ?? [],
                });
            } else if (fs.existsSync(yamlPath)) {
                const cfg = jsyaml.load(fs.readFileSync(yamlPath, "utf-8")) as YamlServiceConfig;
                descriptors.push({
                    name: cfg.name,
                    dir,
                    kind: "yaml",
                    yamlCfg: cfg,
                    provides: cfg.setup ? [`${cfg.name}-setup`] : [],
                    dependencies: cfg.dependsOn ?? [],
                });
            }
        }
    }

    return descriptors;
}

function topologicalSort(descriptors: ServiceDescriptor[]): ServiceDescriptor[] {
    // Map: command name → service name that provides it
    const commandProvider = new Map<string, string>();
    for (const d of descriptors) {
        for (const cmd of d.provides) commandProvider.set(cmd, d.name);
    }

    // Map: service name → service names it depends on
    const deps = new Map<string, Set<string>>();
    for (const d of descriptors) {
        const serviceDeps = new Set<string>();
        for (const cmd of d.dependencies) {
            const provider = commandProvider.get(cmd);
            if (provider && provider !== d.name) serviceDeps.add(provider);
        }
        deps.set(d.name, serviceDeps);
    }

    // Kahn's algorithm
    const byName = new Map(descriptors.map(d => [d.name, d]));
    const indegree = new Map(descriptors.map(d => [d.name, 0]));
    const dependents = new Map<string, string[]>();

    for (const d of descriptors) {
        for (const dep of deps.get(d.name) ?? []) {
            if (!dependents.has(dep)) dependents.set(dep, []);
            dependents.get(dep)!.push(d.name);
            indegree.set(d.name, (indegree.get(d.name) ?? 0) + 1);
        }
    }

    const queue = descriptors.filter(d => (indegree.get(d.name) ?? 0) === 0);
    const sorted: ServiceDescriptor[] = [];

    while (queue.length > 0) {
        const d = queue.shift()!;
        sorted.push(d);
        for (const dependent of dependents.get(d.name) ?? []) {
            const newDeg = (indegree.get(dependent) ?? 0) - 1;
            indegree.set(dependent, newDeg);
            if (newDeg === 0) queue.push(byName.get(dependent)!);
        }
    }

    if (sorted.length !== descriptors.length) {
        const remaining = descriptors.filter(d => !sorted.includes(d)).map(d => d.name);
        throw new Error(`Circular service dependency detected: ${remaining.join(", ")}`);
    }

    return sorted;
}

// ── Public entry point ────────────────────────────────────────────────────────

export function discoverAndRegisterAll(ctx: ServiceContext, baseDirs: string[]): void {
    const descriptors = buildDescriptors(baseDirs);
    const sorted = topologicalSort(descriptors);

    for (const d of sorted) {
        if (d.kind === "typescript" && d.mod) {
            d.mod.register(ctx);
        } else if (d.kind === "yaml" && d.yamlCfg) {
            registerYamlService(d.dir, d.yamlCfg, ctx);
        }
    }
}
