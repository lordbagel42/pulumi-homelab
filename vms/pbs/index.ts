import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import * as command from "@pulumi/command";
import { ip, GATEWAY } from "../../framework";
import type { ServiceContext } from "../../framework";

// ── ServiceModule contract ─────────────────────────────────────────────────────
export const name = "pbs";
export const provides = ["pbs-setup"];
export const dependencies: string[] = [];

export function register(ctx: ServiceContext): void {
    if (!ctx.pbsBackupPassword || !ctx.proxmoxEndpoint || !ctx.proxmoxUsername || !ctx.proxmoxPassword) {
        throw new Error("PBS requires pbsBackupPassword, proxmoxEndpoint, proxmoxUsername, proxmoxPassword in ServiceContext");
    }
    createPbs({
        provider: ctx.provider,
        sshKey: ctx.sshKey,
        sshPrivateKey: ctx.sshPrivateKey,
        debianCloudImageId: ctx.debianCloudImageId,
        cloudInitSnippetId: ctx.cloudInitSnippetId,
        pbsBackupPassword: pulumi.output(ctx.pbsBackupPassword),
        proxmoxEndpoint: pulumi.output(ctx.proxmoxEndpoint),
        proxmoxUsername: pulumi.output(ctx.proxmoxUsername),
        proxmoxPassword: pulumi.output(ctx.proxmoxPassword),
    });
}

const PBS_VMID = 100;
const PBS_IP = ip(PBS_VMID);
const PBS_STORAGE_ID = "pbs";

export interface PbsArgs {
	provider: proxmox.Provider;
	sshKey: string;
	sshPrivateKey: pulumi.Output<string>;
	debianCloudImageId: pulumi.Input<string>;
	cloudInitSnippetId: pulumi.Input<string>;
	pbsBackupPassword: pulumi.Output<string>;
	proxmoxEndpoint: pulumi.Output<string>;
	proxmoxUsername: pulumi.Output<string>;
	proxmoxPassword: pulumi.Output<string>;
}

// Helper that builds an authenticated curl prefix outputting ticket/CSRF
// Both storage registration and backup job share the same auth pattern.
function pveApiScript(endpoint: string): string {
	return `
set -euo pipefail
ENDPOINT="${endpoint}"
ENDPOINT="\${ENDPOINT%/}"
AUTH=$(curl -sf -k -X POST "$ENDPOINT/api2/json/access/ticket" \\
    --data-urlencode "username=$_PVE_USER" \\
    --data-urlencode "password=$_PVE_PASS")
TICKET=$(printf '%s' "$AUTH" | jq -r '.data.ticket')
CSRF=$(printf '%s' "$AUTH" | jq -r '.data.CSRFPreventionToken')
`.trimStart();
}

export function createPbs({
	provider,
	sshKey,
	sshPrivateKey,
	debianCloudImageId,
	cloudInitSnippetId,
	pbsBackupPassword,
	proxmoxEndpoint,
	proxmoxUsername,
	proxmoxPassword,
}: PbsArgs): void {
	const pbsScriptPath = path.join(__dirname, "pbs-setup.sh");

	const pbsVm = new proxmox.VmLegacy("proxmox-backup-server", {
		nodeName: "optiplex",
		vmId: PBS_VMID,
		name: "proxmox-backup-server",

		stopOnDestroy: true,

		agent: {
			enabled: true,
			trim: true,
			type: "virtio",
		},

		cpu: {
			cores: 2,
			type: "x86-64-v2-AES",
		},

		memory: {
			dedicated: 4096,
		},

		disks: [
			{
				datastoreId: "local-lvm",
				interface: "scsi0",
				importFrom: debianCloudImageId,
				size: 12,
				discard: "on",
				ssd: true,
			},
			{
				datastoreId: "hdd",
				interface: "scsi1",
				size: 400,
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
			ipConfigs: [{ ipv4: { address: `${PBS_IP}/24`, gateway: GATEWAY } }],
			userDataFileId: cloudInitSnippetId,
		},

		cdrom: {
			fileId: "none",
		},

		operatingSystem: {
			type: "l26",
		},

		tags: ["backup", "system"],

		onBoot: true,
		started: true,
	}, { provider });

	const pbsSetup = new command.local.Command("pbs-setup", {
		create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
echo "Waiting for SSH on ${PBS_IP}..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${PBS_IP} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no root@${PBS_IP} "PBS_BACKUP_PASSWORD=$_PBS_PASSWORD bash -s" < "${pbsScriptPath}"
rc=$?
rm -f "$key"
exit $rc
		`.trim(),
		environment: {
			_SSH_KEY: sshPrivateKey,
			_PBS_PASSWORD: pbsBackupPassword,
		},
	}, { dependsOn: [pbsVm] });

	const pbsFingerprint = new command.local.Command("pbs-fingerprint", {
		create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
echo "Waiting for PBS certificate..." >&2
for i in $(seq 1 30); do
  fp=$(ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@${PBS_IP} \
    "proxmox-backup-manager cert info 2>/dev/null | grep -i Fingerprint | sed 's/.*: //'" 2>/dev/null)
  if [ -n "$fp" ]; then
    rm -f "$key"
    printf '%s' "$fp"
    exit 0
  fi
  sleep 5
done
rm -f "$key"
exit 1
		`.trim(),
		environment: {
			_SSH_KEY: sshPrivateKey,
		},
	}, { dependsOn: [pbsSetup] });

	// proxmoxve:storage:Pbs and proxmoxve:backup:Job both panic in v8.1.0 due to
	// a nil int assertion in resourceComputeIDOverride. Use the Proxmox REST API directly.
	const commonEnv = {
		_PVE_USER: proxmoxUsername,
		_PVE_PASS: proxmoxPassword,
	};

	const storageRegister = new command.local.Command("pbs-storage-register", {
		interpreter: ["/bin/bash", "-c"],
		create: pulumi.interpolate`
${pveApiScript("$_PVE_ENDPOINT")}
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -k \\
    -H "Cookie: PVEAuthCookie=$TICKET" \\
    "$ENDPOINT/api2/json/storage/${PBS_STORAGE_ID}")
if [ "$STATUS" = "200" ]; then exit 0; fi
curl -sf -k -X POST "$ENDPOINT/api2/json/storage" \\
    -H "CSRFPreventionToken: $CSRF" \\
    -H "Cookie: PVEAuthCookie=$TICKET" \\
    --data-urlencode "storage=${PBS_STORAGE_ID}" \\
    --data-urlencode "type=pbs" \\
    --data-urlencode "server=${PBS_IP}" \\
    --data-urlencode "datastore=main" \\
    --data-urlencode "username=root@pam" \\
    --data-urlencode "password=$_PBS_PASS" \\
    --data-urlencode "fingerprint=${pbsFingerprint.stdout.apply(s => s.trim())}" \\
    --data-urlencode "content=backup" \\
    --data-urlencode "nodes=optiplex" > /dev/null
		`.apply(s => s.trim()),
		delete: pulumi.interpolate`
${pveApiScript("$_PVE_ENDPOINT")}
curl -sf -k -X DELETE "$ENDPOINT/api2/json/storage/${PBS_STORAGE_ID}" \\
    -H "CSRFPreventionToken: $CSRF" \\
    -H "Cookie: PVEAuthCookie=$TICKET" > /dev/null || true
		`.apply(s => s.trim()),
		environment: {
			...commonEnv,
			_PVE_ENDPOINT: proxmoxEndpoint,
			_PBS_PASS: pbsBackupPassword,
		},
	}, { dependsOn: [pbsFingerprint] });

	new command.local.Command("pbs-backup-job", {
		interpreter: ["/bin/bash", "-c"],
		create: pulumi.interpolate`
${pveApiScript("$_PVE_ENDPOINT")}
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -k \\
    -H "Cookie: PVEAuthCookie=$TICKET" \\
    "$ENDPOINT/api2/json/cluster/backup/nightly-backup")
if [ "$STATUS" = "200" ]; then exit 0; fi
curl -sf -k -X POST "$ENDPOINT/api2/json/cluster/backup" \\
    -H "CSRFPreventionToken: $CSRF" \\
    -H "Cookie: PVEAuthCookie=$TICKET" \\
    --data-urlencode "id=nightly-backup" \\
    --data-urlencode "storage=${PBS_STORAGE_ID}" \\
    --data-urlencode "schedule=*-*-* 02:00" \\
    --data-urlencode "all=1" \\
    --data-urlencode "mode=snapshot" \\
    --data-urlencode "compress=zstd" \\
    --data-urlencode "prune-backups=keep-daily=7,keep-weekly=4,keep-monthly=3" > /dev/null
		`.apply(s => s.trim()),
		delete: pulumi.interpolate`
${pveApiScript("$_PVE_ENDPOINT")}
curl -sf -k -X DELETE "$ENDPOINT/api2/json/cluster/backup/nightly-backup" \\
    -H "CSRFPreventionToken: $CSRF" \\
    -H "Cookie: PVEAuthCookie=$TICKET" > /dev/null || true
		`.apply(s => s.trim()),
		environment: {
			...commonEnv,
			_PVE_ENDPOINT: proxmoxEndpoint,
		},
	}, { dependsOn: [storageRegister] });
}
