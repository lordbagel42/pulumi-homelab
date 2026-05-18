import { LXCDefinition } from "../lxc-config";

// IDs 190–199 (hashistack range within system range 0–199)
export const hashistackLxcs: LXCDefinition[] = [
	{
		name: "consul-server",
		vmId: 190,
		nodeName: "optiplex",

		cpuCores: 1,
		memoryMb: 256,
		diskGb: 4,

		ipAddress: "dhcp",
	},

	{
		name: "nomad-server",
		vmId: 191,
		nodeName: "optiplex",

		cpuCores: 4,
		memoryMb: 8192,
		diskGb: 80,

		ipAddress: "dhcp",
	},
];
