import * as fs from "fs";
import * as path from "path";
import * as jsyaml from "js-yaml";
import * as pulumi from "@pulumi/pulumi";
import { ProxmoxMachine } from "./proxmox-machine";
// Re-exported from proxmox-machine.ts rather than declared here: index.ts
// already imports from that module, so declaring it here would make the two
// mutually dependent and could leave LAN_PREFIX undefined at module init.
export { LAN_PREFIX } from "./proxmox-machine";
import { lxcPassword } from "../infisical";
import { InfisicalConfig } from "../infisical";
import { GrafanaConfig } from "../utils/alloy";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as consul from "@pulumi/consul";
import * as nomad from "@pulumi/nomad";

export const GATEWAY = "192.168.0.1";

export function ip(vmid: number): string {
    return `192.168.0.${vmid}`;
}

export interface ServiceContext {
    provider: proxmox.Provider;
    infisicalConfig: InfisicalConfig;
    sshKey: string;
    sshPrivateKey: pulumi.Output<string>;
    vmPassword: pulumi.Output<string>;
    debianCloudImageId: pulumi.Input<string>;
    cloudInitSnippetId: pulumi.Input<string>;
    cloudflaredTunnelToken: pulumi.Input<string>;
    pbsBackupPassword?: pulumi.Input<string>;
    proxmoxEndpoint?: pulumi.Input<string>;
    proxmoxUsername?: pulumi.Input<string>;
    proxmoxPassword?: pulumi.Input<string>;
    oraclePublicIp?: pulumi.Input<string>;
    oraclePrivateKey?: pulumi.Input<string>;
    oracleUser?: pulumi.Input<string>;
    oracleCfTunnelToken?: pulumi.Input<string>;
    oracleNbIp?: pulumi.Input<string>;
    oraclePelicanAppKey?: pulumi.Output<string>;
    oraclePelicanDbPass?: pulumi.Output<string>;
    oraclePelicanDbRootPass?: pulumi.Output<string>;
    grafana?: GrafanaConfig;
    commands: Map<string, pulumi.Resource>;
    /**
     * Consul provider for the homelab datacenter, published by the hashistack
     * module once the Consul server is provisioned. Modules that register
     * Traefik routes should use this rather than building their own, so the
     * registration waits for Consul to actually be up.
     */
    consulProvider?: consul.Provider;
    /**
     * Nomad provider for the homelab cluster, published by the hashistack
     * module once the Nomad server is provisioned.
     */
    nomadProvider?: nomad.Provider;
}

export interface ServiceModule {
    readonly name: string;
    readonly provides?: string[];
    readonly dependencies?: string[];
    register(ctx: ServiceContext): void;
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
    nesting?: boolean;
    privileged?: boolean;
    reverseProxy?: {
        domain: string;
        port: number;
        protected?: boolean;
        entrypoint?: string;
    };
    dependsOn?: string[];
}

function registerYamlService(dir: string, cfg: YamlServiceConfig, ctx: ServiceContext): void {
    new ProxmoxMachine(cfg.name, {
        type: cfg.type || "lxc",
        vmId: cfg.vmid,
        ip: cfg.ip,
        gateway: cfg.gateway,
        cpu: cfg.cpu,
        memory: cfg.memory,
        disk: cfg.disk,
        tags: cfg.tags,
        nesting: cfg.nesting,
        privileged: cfg.privileged,
        sshKeys: [ctx.sshKey],
        password: lxcPassword(cfg.name, ctx.infisicalConfig),
        reverseProxy: cfg.reverseProxy,
        consulProvider: ctx.consulProvider,
    }, { provider: ctx.provider });
}

interface ServiceDescriptor {
    name: string;
    dir: string;
    kind: "typescript" | "yaml";
    mod?: ServiceModule;
    yamlCfg?: YamlServiceConfig;
    provides: string[];
    dependencies: string[];
}

/**
 * Orders descriptors so a module always registers after everything it depends
 * on. Dependencies are capability names (the strings in `provides`), which is
 * also what modules look up in `ctx.commands`; registering out of order leaves
 * those lookups undefined and silently drops the `dependsOn` edge.
 *
 * Unknown dependencies (nothing provides them) are ignored rather than fatal —
 * optional modules like `sandbox` are no-ops unless the stack opts in.
 */
function topologicalSort(descriptors: ServiceDescriptor[]): ServiceDescriptor[] {
    const providerOf = new Map<string, ServiceDescriptor>();
    for (const d of descriptors) {
        for (const capability of d.provides) providerOf.set(capability, d);
    }

    const sorted: ServiceDescriptor[] = [];
    const state = new Map<ServiceDescriptor, "visiting" | "done">();

    const visit = (d: ServiceDescriptor, trail: string[]): void => {
        const seen = state.get(d);
        if (seen === "done") return;
        if (seen === "visiting") {
            throw new Error(
                `circular service dependency: ${[...trail, d.name].join(" → ")}`,
            );
        }

        state.set(d, "visiting");
        for (const dep of d.dependencies) {
            const provider = providerOf.get(dep);
            if (provider && provider !== d) visit(provider, [...trail, d.name]);
        }
        state.set(d, "done");
        sorted.push(d);
    };

    for (const d of descriptors) visit(d, []);
    return sorted;
}

export function discoverAndRegisterAll(ctx: ServiceContext, baseDirs: string[]): void {
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
                const mod = require(tsPath) as ServiceModule;
                if (typeof mod.register !== "function") continue;
                descriptors.push({
                    name: mod.name,
                    dir,
                    kind: "typescript",
                    mod: mod,
                    provides: mod.provides || [],
                    dependencies: mod.dependencies || [],
                });
            } else if (fs.existsSync(yamlPath)) {
                const cfg = jsyaml.load(fs.readFileSync(yamlPath, "utf-8")) as YamlServiceConfig;
                descriptors.push({
                    name: cfg.name,
                    dir,
                    kind: "yaml",
                    yamlCfg: cfg,
                    provides: cfg.name ? [`${cfg.name}-setup`] : [],
                    dependencies: cfg.dependsOn || [],
                });
            }
        }
    }

    for (const d of topologicalSort(descriptors)) {
        if (d.kind === "typescript") {
            d.mod!.register(ctx);
        } else {
            registerYamlService(d.dir, d.yamlCfg!, ctx);
        }
    }
}
