import { VmDefinition } from "../vm-config";

// IDs 400–409 (testing range within VM range 400–599)
export const testingVms: VmDefinition[] = [
	{
		name: "ubuntu-testing",
		vmId: 400,
		nodeName: "optiplex",

		cpuCores: 2,
		memoryMb: 2048,
		diskGb: 8,

		ipAddress: "dhcp",

		category: "testing",
	},
];
