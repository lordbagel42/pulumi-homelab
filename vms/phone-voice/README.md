# Cisco phone voice services on Proxmox

This module adds persistent Codex sessions (611 and 611xxx), Home Assistant
(555), and the single-phone DHCP helper to the existing **optiplex VM 233,
192.168.0.233**. The `huddle-phone` module owns that VM, Asterisk, TFTP and the
huddle bridge. AudioSocket ports are 9092 for Codex, 9093 for Home Assistant,
9094 for huddles, and 9095 for the Slack operator; all listen on loopback.

Dial **0** and say “call me” or a Slack member's name/username. Confirm the
read-back with “yes” or **#**. Numbered matches accept speech or keys 1–5;
**\*** starts a new search. The operator shares the Codex bridge's local speech
models and hands the existing phone connection to Chime after confirmation.
It does not transcribe the ensuing huddle or create a Codex task.
The encrypted `huddle-phone:operatorSecret` is shared through a mode-0600
`state/huddle-operator.json`; `huddle-phone:operatorOwnerId` controls “call me”.

Dial 611 and choose **1 / Proxmox** or **2 / workstation**. Sessions run on
the VM as `codex-phone` or on the workstation as `raygen`, with
`danger-full-access`, approval policy `never`, live web search, and agent tools
enabled, as requested by the owner. New and resumed sessions use the same
settings. The agent has the host account's filesystem/network access, including
Docker access for phone callbacks. Its working directory is
`/var/lib/codex-phone/workspace` on Proxmox; workstation sessions default to
`/home/raygen/Projects/cisco-phone-shenanigans` and can access local repositories.
The database records each session's host. Existing migrated sessions remain on
Proxmox, and callbacks never silently move work between hosts.

Application releases live under `/opt/phone-voice/releases/`; `current` selects
the running release. Debian 12's Python 3.11 venv is `/opt/phone-voice/venv`.
Codex 0.154.0 is installed from its checksum-verified official package, including
the code-mode host and bundled resources required by shell tools.
The VM uses the x86-64-v3 CPU model to expose AVX2 for local speech inference.
The services use Whisper `base.en` with four CPU threads for phone response
latency. The original `small.en` model remains cached and can be selected with
`WHISPER_MODEL` in the source service units when accuracy is preferred to speed.

Private data stays in `/var/lib/codex-phone`, outside releases:

- `state/sessions.sqlite3`: callback numbers, tasks, questions and messages.
- `.codex/`: the migrated ChatGPT login and phone-session conversation histories.
- `models/`: Whisper and Piper speech models.
- `workspace/`: a copy of the phone project for phone-created tasks.
- `home-assistant.json`: the Home Assistant connection configuration.

## Configuration and deployment

The `phone-voice` Pulumi namespace contains `enabled`, secret
`homeAssistantConfig`, `tunnelPublicKey`, and `dhcpEnabled` (default false).
SSH uses the already verified `huddle-phone:sshHostKey` and homelab private key.
No credentials or user histories belong in the public `source/` snapshot.

Refresh source and validate before previewing a targeted update:

```bash
python3 vms/phone-voice/sync-source.py /home/raygen/Projects/cisco-phone-shenanigans
pnpm exec tsc --noEmit
env -u SSH_AUTH_SOCK pulumi preview \
  --target 'urn:pulumi:homelab::pulumi-homelab::command:remote:Command::phone-voice-stage' \
  --target 'urn:pulumi:homelab::pulumi-homelab::command:remote:CopyToRemote::phone-voice-source' \
  --target 'urn:pulumi:homelab::pulumi-homelab::command:remote:Command::phone-voice-setup'
```

After reviewing the preview, run the same targets with `pulumi up`.
Only one stack update can run at a time. Voice updates restart the voice services;
wait for calls and tasks to finish first. Source-copy replacements affect only
release directories; they do not replace the VM or persistent state.

## Initial migration

`migrate-state.py` stages models, a workspace copy, a consistent SQLite snapshot,
and only the phone-created Codex histories. The workstation copy is retained.
It transfers credentials directly over SSH, outside Pulumi logs and state.

```bash
python3 vms/phone-voice/migrate-state.py /home/raygen/Projects/cisco-phone-shenanigans
# After validating the staged VM, while the phone and its tasks are idle:
python3 vms/phone-voice/migrate-state.py /home/raygen/Projects/cisco-phone-shenanigans --final
```

The final pass stops local voice services, takes a fresh backup, remaps saved
working directories, and starts the VM services. Validate them before recording
`/var/lib/codex-phone/migration-complete`; future deployments then restart them.
The import refuses to overwrite a completed migration.

The workstation's `codex-phone-tunnel.service` forwards its existing private
control socket over SSH to the VM. It also forwards the workstation's private
Codex Unix socket back to `/var/lib/codex-phone/workstation-app-server.sock`.
`codex-phone-workstation.service` relays Codex's stdio protocol over that socket
and owns the workstation app-server. The VM connects when a workstation task
is requested; telephone hangup does not close that connection.
The dedicated SSH key accepts forwarding; shell commands and agent forwarding
are disabled. Neither control endpoint listens on the LAN.

Proxmox tasks keep running when the workstation or tunnel stops. Workstation
tasks require that host, and the tunnel must be connected for phone control.
Loss of the connection stops its workstation process group and reports the
failure instead of replaying commands or falling back to another host. The
saved thread remains on its original host. Restarting the bridge or tunnel
interrupts active workstation work; hanging up the phone does not.

Stop the workstation's `sccp-recovery-dhcp` before setting
`phone-voice:dhcpEnabled=true` and deploying the `phone-dhcp` resource. That helper
answers only MAC `0C:27:24:31:7F:2E`, advertises TFTP `.233`, and gives the phone
its existing `.197` address. It has no general DHCP pool. If the router later
supplies option 150 or the phone is set to Alternate TFTP `.233`, disable this
helper. Reboot the handset normally; do not factory-reset it.

## Operation

On VM 233:

```bash
systemctl status codex-phone-bridge home-assistant-phone cisco-phone-dhcp
journalctl -u codex-phone-bridge -f
docker exec sccp-pbx asterisk -rx 'sccp show devices'
```

On the workstation, existing `voice/phone_cli.py` and the `codex_phone` MCP
continue to use `voice/state/control.sock`, now forwarded to the VM. The
workstation host and tunnel are enabled user services:

```bash
systemctl --user status codex-phone-workstation codex-phone-tunnel
```

Their source units live in the original phone project's `voice/` directory.
Restarting either interrupts active workstation tasks, so check for work first.

## Migration verification — September 20, 2026

The original 17 callback entries and all 12 existing managed Codex histories
were migrated. Those histories resumed with full access and approval policy
`never`. Verification sessions keep their allocated numbers.

The VM passed real shell execution across AudioSocket hangup, callback playback
of the saved result, spoken and keypad interruption, and explicit task
interruption preserving the same conversation. A real stdio MCP question
returned its keypad answer without a second confirmation menu. Home Assistant
returned a live spoken-text response through its cluster-hosted ChatGPT adapter.
All 50 local regression tests and the TypeScript check passed.

Dual-host live verification also passed: the 611 menu selected each host,
shell commands returned the corresponding hostname and project directory,
both jobs finished after hangup, and callbacks retained their host and thread.
A workstation Codex clarification received its keypad answer, and double-star
stopped a real workstation turn. With the workstation host stopped, a Proxmox
shell task still finished while the local task reported it was unavailable.
After restarting the host, an explicit “Continue” retried the saved local task
on the workstation. No task was silently moved to another machine.

`verify-hosts.py` repeats the AudioSocket checks on VM 233 without ringing the
handset. Copy it to `/tmp` and run as `codex-phone` from its workspace, with
`PYTHONPATH=/opt/phone-voice/current:/opt/phone-voice/current/tests`, using the
production venv Python. Run only while the phone and its tasks are idle.

The owner set Alternate TFTP to `192.168.0.233`. The VM served the handset's
configuration at 06:54 UTC and the phone registered again. The workstation's
PBX, TFTP, recovery DHCP, old Codex voice, Home Assistant voice and old ChatGPT
gateway are stopped. The old TFTP container's restart policy is disabled.
The handset no longer depends on the workstation for boot files or calls to
Proxmox sessions. `phone-voice:dhcpEnabled` is now false because Alternate TFTP
is configured on the handset.

## Recovery

Back up VM 233, including `/var/lib/codex-phone` and Docker volumes. Never run the
old and new voice workers against separate copies of the same live sessions.
To roll back, stop the VM voice services and DHCP helper, stop the local tunnel,
point the phone's TFTP/PBX back to `.105`, restore the desired local snapshot if
needed, and start the original local services/containers. Changes made after
cutover must be copied back deliberately before rollback.
