# Slack huddles on the Cisco phone

This module deploys the phone project's Asterisk/SCCP PBX, TFTP server and native
Amazon Chime SDK huddle bridge together on **optiplex, VM 233, 192.168.0.233**.
It uses the existing Debian cloud image and cloud-init snippet, 4 CPUs, 4 GiB RAM
and a 32 GiB disk. Its x86-64-v3 CPU exposes AVX2 for speech inference.
The VM is protected against accidental Pulumi deletion.

The `project/` directory is a deployment snapshot of
`/home/raygen/Projects/cisco-phone-shenanigans`. It includes the phone provisioning
and firmware files, pinned bridge dependencies, runtime and tests. Credentials,
dependency installations, build output and runtime state are excluded.

## Configuration

All settings are in the `huddle-phone` Pulumi namespace:

| Key | Purpose |
| --- | --- |
| `enabled` | Register this module; defaults to false |
| `sshHostKey` | Guest SSH host key, enrolled through Proxmox before provisioning |
| `workspace` | Slack hostname/subdomain; supports `hackclub.enterprise` |
| `teamId` | Channel workspace ID (`T...`), not the enterprise ID |
| `enterpriseId` | Enterprise Grid organization ID (`E...`), when applicable |
| `channelId` | The single channel to watch |
| `clientToken` | **Secret:** the Slack user's `xoxc` token |
| `cookie` | **Secret:** `d=xoxd-...` from that same session |
| `amiSecret` | **Secret:** 24–128 URL-safe letters, numbers, hyphens or underscores |
| `dryRun` | Log detections without calling; defaults to true |
| `incomingInvites` | Ring for direct huddle invitations to the Slack account; defaults to true |
| `phoneChannel` | Defaults to `SCCP/6738` |

Use `pulumi config set --secret huddle-phone:clientToken` and its hidden prompt
for secret values. Do the same for `cookie` and `amiSecret`. Never commit an
unencrypted session token. The module uses secret Outputs and SSH stdin to create
a root-only `.env`; secrets are not placed in command arguments or logged.

## Deploy

From the repository root, after setting the configuration:

```bash
pulumi config set huddle-phone:enabled true
bash vms/huddle-phone/pulumi-service.sh preview
bash vms/huddle-phone/pulumi-service.sh up
```

The first deployment creates just the VM. Enrol its SSH identity through the
authenticated Proxmox guest agent, then preview and run the same update again:

```bash
python3 vms/huddle-phone/enroll-host.py
```

The helper clears `SSH_AUTH_SOCK`, since the command provider attempts to use
that agent even when a private key is supplied. Authentication uses the encrypted
homelab SSH key. It targets only this module's seven resources; it deliberately
does not select every dependent service in the stack.

The second deployment installs Docker, uploads the project, injects configuration,
builds the images, starts the PBX and performs the Slack/AMI doctor check before
starting the watcher. It fails the deployment if `/healthz` does not become healthy.
Future ordinary stack updates discover this module automatically.

The targeted commands above avoid deploying unrelated edits already in the stack.
Review the preview before each update. Run only one active huddle bridge.

## Connect the handset

The account can also be called directly: start a huddle in its DM or invite it
from another huddle. The phone rings, and answering connects to that exact room.
Incoming invitations use the Enterprise-compatible user WebSocket; the health
endpoint reports `invitations_connected`. A fresh invitation may ring again
after a missed call, while duplicate delivery does not.

The deployed copy of `tftp/files/*.cnf.xml` points to `192.168.0.233`; the original
workstation configuration is untouched. On the existing Cisco phone, set
**Alternate TFTP = Yes** and **TFTP Server 1 = 192.168.0.233**, then reboot normally.
Do not factory-reset the phone. Confirm registration and try extension 600's
echo test before starting a new Slack huddle.

If `dryRun` is true, set `pulumi config set huddle-phone:dryRun false` and deploy
again before testing calls. Start a fresh huddle: persisted deduplication means
a room already observed in dry run will not ring again. The account joins through
the Chime SDK only after the handset answers. Slack's private huddle API still
requires an end-to-end audio test with another participant.

This module manages the PBX/TFTP and huddle bridge. The companion
[phone-voice module](../phone-voice/README.md) migrates the Home Assistant/Codex
voice services for extensions 555/611 onto the same VM. Huddles use AudioSocket
port 9094 so they do not conflict with Home Assistant on 9093.
No Cloudflare route, public port, AWS resource or Slack app is created.

## Operation and updates

Outgoing huddles: dial **0** on the Cisco phone, say a member's name, then
confirm it. Say “call me” to use `huddle-phone:operatorOwnerId`. The companion
voice service shares the encrypted `huddle-phone:operatorSecret` through its
private configuration; no Slack credentials leave the huddle container.
The operator's AudioSocket port is 9095 and its control API is loopback-only.
On the VM, exact Slack-ID dialing is also available:

```bash
docker exec huddle-phone-huddle-phone-1 python -m huddle_phone lookup 'name'
docker exec huddle-phone-huddle-phone-1 python -m huddle_phone dial U0123456789
```

The CLI rings the phone first, then starts the DM huddle and invites that
member after answer. The voice operator connects the already answered handset.

SSH to `192.168.0.233` using the existing homelab key. Commands on the VM:

```bash
cd /opt/huddle-phone/current
docker compose -p huddle-phone -f docker-compose.yml -f docker-compose.huddle.yml logs --tail=100 huddle-phone
docker exec sccp-pbx asterisk -rx 'sccp show devices'
curl -f http://127.0.0.1:8099/healthz
```

Refresh the tracked deployment snapshot after editing the original application:

```bash
python3 vms/huddle-phone/sync-project.py /home/raygen/Projects/cisco-phone-shenanigans
pnpm exec tsc --noEmit
```

Changed source creates a new release directory; changed credentials also trigger
redeployment. The Compose project name stays `huddle-phone`, so its named SQLite
volume survives both source and credential updates. Back up the VM and named
volume; do not use `docker compose down -v`. The existing cluster-wide backup job
can cover VM 233. Restart policies bring the services back after a VM reboot.

Refreshing an expired Slack session only needs the two secret config values and
another deployment. Use the [application guide](project/huddle-phone/README.md)
for the private API, call behavior and troubleshooting.
