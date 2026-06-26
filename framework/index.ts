import * as fs from "fs";
import * as path from "path";
import * as jsyaml from "js-yaml";
import * as pulumi from "@pulumi/pulumi";
import { ProxmoxMachine } from "./proxmox-machine";
import { lxcPassword } from "../infisical";
import { InfisicalConfig } from "../infisical";
import { GrafanaConfig } from "../utils/alloy";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

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

interface ServiceDescriptor {
    name: string;
    dir: string;
    kind: "typescript" | "yaml";
    mod?: ServiceModule;
    yamlCfg?: YamlServiceConfig;
    provides: string[];
    dependencies: string[];
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
    }, { provider: ctx.provider });
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
                    provides: cfg.name ? [`${cfg.name}-setup`] : [],
                    dependencies: cfg.dependsOn || [],
                });
            }
        }
    }

    return descriptors;
}

function topologicalSort(descriptors: ServiceDescriptor[]): ServiceDescriptor[] {
    const commandProvider = new Map<string, string>();
    for (const d of descriptors) {
        for (const cmd of d.provides) commandProvider.set(cmd, d.name);
    }

    const deps = new Map<string, Set<string>>();
    for (const d of descriptors) {
        const serviceDeps = new Set<string>();
        for (const cmd of d.dependencies) {
            const provider = commandProvider.get(cmd);
            if (provider && provider !== d.name) serviceDeps.add(provider);
        }
        deps.set(d.name, serviceDeps);
    }

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
