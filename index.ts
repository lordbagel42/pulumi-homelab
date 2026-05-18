import * as fs from "fs";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

import { allVms as vms, allLxcs as lxcs } from "./architecture";

const config = new pulumi.Config();

const endpoint = config.requireSecret("PROXMOX_ENDPOINT");

const provider = new proxmox.Provider("proxmox", {
	endpoint: endpoint,
	username: config.requireSecret("PROXMOX_USERNAME"),
	password: config.requireSecret("PROXMOX_PASSWORD"),
	insecure: config.getBoolean("PROXMOX_INSECURE") ?? false,
	ssh: {
		nodes: [{
			name: "optiplex",
			address: endpoint.apply(e => new URL(e).hostname),
		}],
	},
});

const sshKey = fs.readFileSync("./proxmox-pulumi.pub", "utf-8").trim();
const vmPassword = config.requireSecret("PROXMOX_USER_ACCOUNT_PASSWORD");

const vmCloudInitSnippet = new proxmox.FileLegacy("vm-cloud-init", {
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
`,
	},
}, { provider });

const debianCloudImage = new proxmox.download.File("debian-cloud-image", {
	contentType: "import",
	datastoreId: "local",
	nodeName: "optiplex",
	url: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
	fileName: "debian-12-genericcloud-amd64.qcow2",
}, { provider });

for (const vm of vms) {
	new proxmox.VmLegacy(
		vm.name,
		{
			nodeName: vm.nodeName,
			vmId: vm.vmId,
			name: vm.name,

			stopOnDestroy: true,

			agent: {
				enabled: true,
				trim: true,
				type: "virtio",
			},

			cpu: {
				cores: vm.cpuCores,
				type: "x86-64-v2-AES",
			},

			memory: {
				dedicated: vm.memoryMb,
			},

			disks: [
				{
					datastoreId: "local-lvm",
					interface: "scsi0",
					importFrom: debianCloudImage.id,
					size: vm.diskGb,
					discard: "on",
					ssd: true,
				},
			],

			networkDevices: [
				{
					bridge: "vmbr0",
					model: "virtio",
				},
			],

			initialization: {
				datastoreId: "local-lvm",

				ipConfigs: [
					vm.ipAddress === "dhcp"
						? { ipv4: { address: "dhcp" } }
						: { ipv4: { address: vm.ipAddress, gateway: vm.gateway } },
				],

				userDataFileId: vmCloudInitSnippet.id,
			},

			cdrom: {
				fileId: "none",
			},

			operatingSystem: {
				type: "l26",
			},

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

			cpu: {
				cores: lxc.cpuCores,
			},

			memory: {
				dedicated: lxc.memoryMb,
			},

			disk: {
				datastoreId: "local-lvm",
				size: lxc.diskGb,
			},

			operatingSystem: {
				templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
				type: "debian",
			},

			networkInterfaces: [
				{
					name: "veth0",
					bridge: "vmbr0",
				},
			],

			initialization: {
				hostname: lxc.name,

				ipConfigs: [
					lxc.ipAddress === "dhcp"
						? { ipv4: { address: "dhcp" } }
						: { ipv4: { address: lxc.ipAddress, gateway: lxc.gateway } },
				],

				userAccount: {
					password: vmPassword,
					keys: [sshKey],
				},
			},

			startOnBoot: true,
			started: true,
		},
		{ provider },
	);
}
