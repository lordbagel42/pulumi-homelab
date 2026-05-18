import { VmDefinition } from "../vm-config";

// IDs 100–199 (DHCP / ephemeral VMs)
export const testingVms: VmDefinition[] = [
	{
		name: "ubuntu-testing",
		vmId: 100,
		nodeName: "optiplex",

		cpuCores: 2,
		memoryMb: 2048,
		diskGb: 8,

		ipAddress: "dhcp",

		category: "testing",
	},
];
