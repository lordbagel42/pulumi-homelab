import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";

const CONSUL_VERSION = "1.20.1";
const NOMAD_VERSION = "1.9.3";

const CONSUL_VMID = 201;
const NOMAD_VMID = 202;
const CONSUL_IP = `192.168.0.${CONSUL_VMID}`;
const NOMAD_IP = `192.168.0.${NOMAD_VMID}`;
const GATEWAY = "192.168.0.1";

export interface HashistackArgs {
	provider: proxmox.Provider;
	vmPassword: pulumi.Output<string>;
	sshKey: string;
	sshPrivateKey: pulumi.Output<string>;
}

function sshSetup(
	name: string,
	host: string,
	scriptPath: string,
	env: Record<string, string>,
	sshPrivateKey: pulumi.Output<string>,
	dependsOn: pulumi.Resource[],
): command.local.Command {
	const remoteEnv = Object.entries(env)
		.map(([k, v]) => `${k}=${v}`)
		.join(" ");

	return new command.local.Command(name, {
		create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s' "$_SSH_KEY" > "$key"
ssh -i "$key" -o StrictHostKeyChecking=no root@${host} "${remoteEnv} bash -s" < "${scriptPath}"
rm -f "$key"
		`.trim(),
		environment: {
			_SSH_KEY: sshPrivateKey,
		},
	}, { dependsOn });
}

export function createHashistack({
	provider,
	vmPassword,
	sshKey,
	sshPrivateKey,
}: HashistackArgs): void {
	const consulScriptPath = path.join(__dirname, "consul-setup.sh");
	const nomadScriptPath = path.join(__dirname, "nomad-setup.sh");

	const consulContainer = new proxmox.ContainerLegacy("consul-server", {
		nodeName: "optiplex",
		vmId: CONSUL_VMID,

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
	}, { provider });

	sshSetup("consul-setup", CONSUL_IP, consulScriptPath, {
		CONSUL_IP,
		CONSUL_VERSION,
	}, sshPrivateKey, [consulContainer]);

	const nomadContainer = new proxmox.ContainerLegacy("nomad-server", {
		nodeName: "optiplex",
		vmId: NOMAD_VMID,

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
	}, { provider });

	sshSetup("nomad-setup", NOMAD_IP, nomadScriptPath, {
		CONSUL_IP,
		CONSUL_VERSION,
		NOMAD_IP,
		NOMAD_VERSION,
	}, sshPrivateKey, [nomadContainer, consulContainer]);
}
