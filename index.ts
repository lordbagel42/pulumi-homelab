import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

import { allVms as vms, allLxcs as lxcs } from "./architecture";
import { InfisicalConfig, readSecret } from "./infisical";
import { discoverAndRegisterAll, ServiceContext } from "./framework";
import type { GrafanaConfig } from "./utils/alloy";

const config = new pulumi.Config();

const isHosted = process.env.HOSTED === "true";
const endpoint = isHosted
    ? config.requireSecret("PROXMOX_NETBIRD_ENDPOINT")
    : config.requireSecret("PROXMOX_ENDPOINT");

const provider = new proxmox.Provider("proxmox", {
    endpoint: endpoint,
    username: config.requireSecret("PROXMOX_USERNAME"),
    password: config.requireSecret("PROXMOX_PASSWORD"),
    insecure: config.getBoolean("PROXMOX_INSECURE") ?? false,
    ssh: {
        nodes: [{
            name: "optiplex",
            address: endpoint.apply((e) => new URL(e).hostname),
        }],
    },
});

const sshKey = fs.readFileSync("./proxmox-pulumi.pub", "utf-8").trim();
const sshPrivateKey = config.requireSecret("SSH_PRIVATE_KEY");
const vmPassword = config.requireSecret("PROXMOX_USER_ACCOUNT_PASSWORD");
const pbsBackupPassword = config.requireSecret("PBS_BACKUP_PASSWORD");
const cloudflaredTunnelToken = config.requireSecret("CLOUDFLARE_TUNNEL_TOKEN");

const infisicalConfig: InfisicalConfig = {
    clientId: config.requireSecret("INFISICAL_CLIENT_ID"),
    clientSecret: config.requireSecret("INFISICAL_CLIENT_SECRET"),
    projectId: config.requireSecret("INFISICAL_PROJECT_ID"),
    environment: config.get("INFISICAL_ENVIRONMENT") ?? "prod",
    secretPath: config.get("INFISICAL_SECRET_PATH") ?? "/proxmox",
    host: config.get("INFISICAL_HOST"),
};

const grafanaInfisicalConfig: InfisicalConfig = { ...infisicalConfig, secretPath: "/grafana" };
const grafana: GrafanaConfig = {
    logsUrl:       readSecret("GCLOUD_HOSTED_LOGS_URL",    grafanaInfisicalConfig),
    logsId:        readSecret("GCLOUD_HOSTED_LOGS_ID",     grafanaInfisicalConfig),
    metricsUrl:    readSecret("GCLOUD_HOSTED_METRICS_URL", grafanaInfisicalConfig),
    metricsId:     readSecret("GCLOUD_HOSTED_METRICS_ID",  grafanaInfisicalConfig),
    apiKey:        readSecret("GCLOUD_RW_API_KEY",         grafanaInfisicalConfig),
    scrapeInterval: readSecret("GCLOUD_SCRAPE_INTERVAL",   grafanaInfisicalConfig),
};

const oracleInfisicalConfig: InfisicalConfig = { ...infisicalConfig, secretPath: "/oracle" };
const oraclePublicIp = readSecret("ORACLE_PUBLIC_IP", oracleInfisicalConfig);
const oraclePrivateKey = readSecret("ORACLE_PRIVATE_KEY", oracleInfisicalConfig);
const oracleUser = readSecret("ORACLE_USER", oracleInfisicalConfig);
const oracleCfTunnelToken = readSecret("ORACLE_CLOUDFLARE_TUNNEL_TOKEN", oracleInfisicalConfig);
const oracleNbIp = readSecret("ORACLE_NB_IP", oracleInfisicalConfig);
const oraclePelicanAppKey = readSecret("PELICAN_APP_KEY", oracleInfisicalConfig);
const oraclePelicanDbPass = readSecret("PELICAN_DB_PASSWORD", oracleInfisicalConfig);
const oraclePelicanDbRootPass = readSecret("PELICAN_DB_ROOT_PASSWORD", oracleInfisicalConfig);

const vmCloudInitSnippet = new proxmox.FileLegacy(
    "vm-cloud-init",
    {
        contentType: "snippets",
        datastoreId: "local",
        nodeName: "optiplex",
        sourceRaw: {
            fileName: "vm-cloud-init.yaml",
            data: pulumi.interpolate`#cloud-config
users:
  - name: sylvie
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    ssh_authorized_keys:
      - ${sshKey}
  - name: root
    ssh_authorized_keys:
      - ${sshKey}
chpasswd:
  users:
    - name: sylvie
      password: ${vmPassword}
      type: text
  expire: false
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
  - sed -i '/PermitRootLogin/d' /etc/ssh/sshd_config
  - echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
  - sed -i '/^#\?UseDNS/d' /etc/ssh/sshd_config
  - echo 'UseDNS no' >> /etc/ssh/sshd_config
  - systemctl restart sshd
`,
        },
    },
    { provider },
);

const debianCloudImage = new proxmox.download.File(
    "debian-cloud-image",
    {
        contentType: "import",
        datastoreId: "local",
        nodeName: "optiplex",
        url: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        fileName: "debian-12-genericcloud-amd64.qcow2",
    },
    { provider },
);

// ── Static VMs/LXCs from architecture.ts ──────────────────────────────────────

for (const vm of vms) {
    new proxmox.VmLegacy(
        vm.name,
        {
            nodeName: vm.nodeName,
            vmId: vm.vmId,
            name: vm.name,
            stopOnDestroy: true,
            agent: { enabled: true, trim: true, type: "virtio" },
            cpu: { cores: vm.cpuCores, type: "x86-64-v2-AES" },
            memory: { dedicated: vm.memoryMb },
            disks: [{
                datastoreId: "local-lvm",
                interface: "scsi0",
                importFrom: debianCloudImage.id,
                size: vm.diskGb,
                discard: "on",
                ssd: true,
            }],
            networkDevices: [{ bridge: "vmbr0", model: "virtio" }],
            initialization: {
                datastoreId: "local-lvm",
                ipConfigs: [
                    vm.ipAddress === "dhcp"
                        ? { ipv4: { address: "dhcp" } }
                        : { ipv4: { address: vm.ipAddress, gateway: vm.gateway } },
                ],
                userDataFileId: vmCloudInitSnippet.id,
            },
            cdrom: { fileId: "none" },
            operatingSystem: { type: "l26" },
            tags: [vm.category, ...(vm.tags ?? [])],
            onBoot: true,
            started: true,
        },
        { provider },
    );
}

for (const lxc of lxcs) {
    new proxmox.ContainerLegacy(
        lxc.name,
        {
            nodeName: lxc.nodeName,
            vmId: lxc.vmId,
            cpu: { cores: lxc.cpuCores },
            memory: { dedicated: lxc.memoryMb },
            disk: { datastoreId: "local-lvm", size: lxc.diskGb },
            operatingSystem: {
                templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
                type: "debian",
            },
            networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
            initialization: {
                hostname: lxc.name,
                ipConfigs: [
                    lxc.ipAddress === "dhcp"
                        ? { ipv4: { address: "dhcp" } }
                        : { ipv4: { address: lxc.ipAddress, gateway: lxc.gateway } },
                ],
                userAccount: { password: vmPassword, keys: [sshKey] },
            },
            tags: [lxc.category, ...(lxc.tags ?? [])],
            startOnBoot: true,
            started: true,
        },
        { provider },
    );
}

// ── Service discovery ──────────────────────────────────────────────────────────

const ctx: ServiceContext = {
    provider,
    infisicalConfig,
    grafana,
    sshKey,
    sshPrivateKey,
    vmPassword,
    debianCloudImageId: debianCloudImage.id,
    cloudInitSnippetId: vmCloudInitSnippet.id,
    cloudflaredTunnelToken,
    pbsBackupPassword,
    proxmoxEndpoint: endpoint,
    proxmoxUsername: config.requireSecret("PROXMOX_USERNAME"),
    proxmoxPassword: config.requireSecret("PROXMOX_PASSWORD"),
    oraclePublicIp,
    oraclePrivateKey,
    oracleUser,
    oracleCfTunnelToken,
    oracleNbIp,
    oraclePelicanAppKey,
    oraclePelicanDbPass,
    oraclePelicanDbRootPass,
    commands: new Map(),
};

discoverAndRegisterAll(ctx, [
    path.join(__dirname, "lxcs"),
    path.join(__dirname, "vms"),
]);
