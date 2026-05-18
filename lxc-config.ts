export interface LXCDefinition {
	name: string;
	vmId: number;
	nodeName: string;

	cpuCores: number;
	memoryMb: number;
	diskGb: number;

	ipAddress: string;
	gateway?: string;
}
