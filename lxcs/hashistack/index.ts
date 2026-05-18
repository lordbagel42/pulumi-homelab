import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";

const CONSUL_VERSION = "1.20.1";
const NOMAD_VERSION = "1.9.3";

const CONSUL_IP = "192.168.0.190";
const NOMAD_IP = "192.168.0.191";
const GATEWAY = "192.168.0.1";

export interface HashistackArgs {
	provider: proxmox.Provider;
	vmPassword: pulumi.Output<string>;
	sshKey: string;
	sshPrivateKey: pulumi.Output<string>;
}

export function createHashistack({
	provider,
	vmPassword,
	sshKey,
	sshPrivateKey,
}: HashistackArgs): void {
	const consulScript = fs.readFileSync(
		path.join(__dirname, "consul-setup.sh"),
		"utf-8",
	);
	const nomadScript = fs.readFileSync(
		path.join(__dirname, "nomad-setup.sh"),
		"utf-8",
	);

	const mkConnection = (host: string) => ({
		host,
		user: "root",
		privateKey: sshPrivateKey,
		dialErrorLimit: 30,
		perDialTimeout: 15,
	});

	const consulContainer = new proxmox.ContainerLegacy(
		"consul-server",
		{
			nodeName: "optiplex",
			vmId: 190,

			cpu: { cores: 1 },
			memory: { dedicated: 256 },
			disk: { datastoreId: "local-lvm", size: 4 },

			operatingSystem: {
				templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
				type: "debian",
			},

			networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],

			initialization: {
				hostname: "consul-server",
				ipConfigs: [{ ipv4: { address: `${CONSUL_IP}/24`, gateway: GATEWAY } }],
				userAccount: { password: vmPassword, keys: [sshKey] },
			},

			tags: ["hashistack", "system"],

			startOnBoot: true,
			started: true,
		},
		{ provider },
	);

	new command.remote.Command(
		"consul-setup",
		{
			connection: mkConnection(CONSUL_IP),
			create: consulScript,
			environment: {
				CONSUL_IP,
				CONSUL_VERSION,
			},
		},
		{ dependsOn: [consulContainer] },
	);

	const nomadContainer = new proxmox.ContainerLegacy(
		"nomad-server",
		{
			nodeName: "optiplex",
			vmId: 191,

			cpu: { cores: 4 },
			memory: { dedicated: 8192 },
			disk: { datastoreId: "local-lvm", size: 80 },

			operatingSystem: {
				templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
				type: "debian",
			},

			networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],

			initialization: {
				hostname: "nomad-server",
				ipConfigs: [{ ipv4: { address: `${NOMAD_IP}/24`, gateway: GATEWAY } }],
				userAccount: { password: vmPassword, keys: [sshKey] },
			},

			tags: ["hashistack", "system"],

			startOnBoot: true,
			started: true,
		},
		{ provider },
	);

	new command.remote.Command(
		"nomad-setup",
		{
			connection: mkConnection(NOMAD_IP),
			create: nomadScript,
			environment: {
				CONSUL_IP,
				CONSUL_VERSION,
				NOMAD_IP,
				NOMAD_VERSION,
			},
		},
		{ dependsOn: [nomadContainer, consulContainer] },
	);
}
