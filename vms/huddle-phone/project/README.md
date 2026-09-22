# Cisco SCCP lab PBX

Make Cisco IP phones call each other with a self-hosted PBX.

The running phone system is on **Proxmox VM 233 (192.168.0.233)**. Its shared
dashboard is **[Switchboard](http://192.168.0.233:8088)**: configure speech,
manage Amp sessions and callbacks, start operators, connect Slack huddles,
and publish optional directories to the handset. See the
[platform and API guide](phone-platform/README.md).

The Compose instructions below describe the original local lab setup. Production
deployment is managed in `/home/raygen/homelab/pulumi-homelab/vms/phone-platform`,
`vms/phone-voice`, and `vms/huddle-phone`.

The phone on the bench (`CP-7945G`, MAC `0C2724317F2E`, ext **6738**) runs
**SCCP** ("Skinny") firmware. This PBX speaks SCCP natively via Asterisk +
[`chan-sccp-b`](https://github.com/chan-sccp/chan-sccp).

## Architecture

```
Cisco 7945G  --TFTP(69)-->  dnsmasq (serves SEP<MAC>.cnf.xml)
     |                          production PBX -> 192.168.0.233:2000
     +--SCCP(2000)/RTP(10000-20000)-->  Asterisk + chan_sccp  (call routing)
```

Two containers, both on the host network (SCCP/TFTP/RTP must reach the LAN
without NAT):

| Service | Image | Purpose |
|---------|-------|---------|
| `pbx`   | `pbx-sccp` (Asterisk 20 + chan-sccp-b 4.3.5) | registration + call routing |
| `tftp`  | `sccp-tftp` (dnsmasq, TFTP only) | serves config and recovery firmware |

The local source template's `PBX_IP` is **192.168.0.105**; Proxmox provisioning
sets the deployed phone configuration to **192.168.0.233**. Change it in
`tftp/files/*.cnf.xml`, `asterisk/sccp.conf` is IP-agnostic, and `add-phone.sh`
(`PBX_IP=` env) if the host address changes.

## Run

```bash
docker compose up -d --build
docker exec sccp-pbx asterisk -rx "sccp show devices"   # registration state
docker exec sccp-pbx asterisk -rx "sccp show lines"
```

Logs / CLI:

```bash
docker exec -it sccp-pbx asterisk -rvvv     # live console
docker compose logs -f pbx
```

## Point a phone at this PBX (one-time, on the handset)

The bench phone has recovered its firmware and registered successfully.
For another previously managed phone, tell it our TFTP address and clear
its old trust list if that list prevents it from accepting our config.

1. **Set TFTP server**
   `Settings` (gear) -> `Network Configuration` -> `IPv4 Configuration`.
   Press `**#` to unlock editing.
   Set `Alternate TFTP` = **Yes**, `TFTP Server 1` = **192.168.0.105**.
   Save. Back out.
2. **Clear the old ITL** (accept our unsigned config)
   `Settings` -> `Security Configuration` -> `Trust List` -> erase the ITL.
   A factory reset also erases the phone application and the alternate TFTP
   setting. Prepare firmware and DHCP option 150 before using that reset;
   the recovery loader cannot open the normal Settings menu. See
   [factory-reset recovery](recovery/README.md).

The phone reboots, pulls `SEP0C2724317F2E.cnf.xml` from TFTP, and registers.
`sccp show devices` then shows it `Registered`.

Dial **555** to control Home Assistant, **611** to speak with Amp, **6739** to reach the second phone,
**600** for an echo test, or **602** for a "hello world" prompt.

## Home Assistant

Dial **555**, wait for the greeting, and speak a command such as “turn on the
kitchen lights.” The phone reads Home Assistant's response back to you.
Connect your server once with `voice/.venv/bin/python voice/configure_home_assistant.py`
and expose the desired devices to Assist. See [Home Assistant setup and
service instructions](voice/HOME_ASSISTANT.md).

The installed **ChatGPT** agent runs through a bridge in Proxmox LXC **213**,
signed in to ChatGPT through the browser, with no OpenAI API key. It is selected
for 555 and the preferred Home Assistant Assist profile. Speech recognition and
synthesis remain local. See [ChatGPT connection details](voice/CHATGPT.md).

## Talk to Amp

The existing `codex-phone-bridge.service` now connects new calls to **611** to
the configured Amp runner. Switchboard retains the native Amp thread ID and audit
link. Each session gets a persistent callback
extension such as **611005**. Hanging up leaves its task running. Talking over
a reply or pressing **\*** cuts off speech; **\*\*** stops the task while
keeping its history, once the runner acknowledges cancellation. The Amp runner
plugin lets Amp call this handset for clarification. Existing Codex callback
numbers remain associated with their original histories.
See [Amp configuration](voice/AMP.md) for runner authentication and plugin setup,
and [ElevenLabs Speech Engine](phone-platform/SPEECH-ENGINE.md) for streaming
speech. Local Whisper/Piper remain available when cloud credits are exhausted.
See [voice bridge instructions](voice/README.md) for service controls,
calling the phone from the computer, and verification.

## Recover a factory-reset 7945G

The TFTP directory now includes the SCCP 9.4(2)SR3 recovery firmware and
`term45.default.loads`. The router must advertise this host as the TFTP
server using DHCP option 150. For the current bench phone, an optional,
temporary DHCP service is restricted to MAC `0C2724317F2E` and its observed
address `192.168.0.197`. See [recovery instructions and firmware
provenance](recovery/README.md) before enabling it on a future occasion.

## Add another phone

```bash
./add-phone.sh <MAC> <EXTENSION> [MODEL]
# e.g.
./add-phone.sh 00:11:22:33:44:55 6739 7945
```

This writes the phone's TFTP config, appends a device + line to
`asterisk/sccp.conf`, and hot-reloads Asterisk. The dialplan (`_6XXX`) already
routes any `6xxx` extension, so no dialplan edit is needed. Then do the
handset steps above on the new phone. Any two registered phones can call each
other by dialing the other's extension.

## Forward Slack huddles to the phone

The optional [huddle-phone selfbot](huddle-phone/README.md) watches one Slack
channel, rings extension **6738**, and connects two-way audio using the
**Amazon Chime SDK directly in the native huddle**. It uses your Slack user
session with the private `rooms.join` API. Deployment instructions and a
Docker Compose override are included for running it with this PBX in a
Proxmox Linux VM. Start with its dry-run mode before enabling calls.

## Making it truly plug-and-play (optional)

Setting TFTP by hand per phone is only needed because the LAN's DHCP server
(the router at `192.168.0.1`) doesn't advertise a TFTP server. If you can set
**DHCP option 150 = 192.168.0.105** on that router (or on the phone VLAN's DHCP
scope), any Cisco phone plugged in auto-discovers this PBX with no handset
config — you'd still add its MAC via `add-phone.sh`.

## Migrating to Kubernetes later

- Both images are plain containers; push `pbx-sccp` and `sccp-tftp` to a
  registry.
- SCCP/RTP need the pod reachable on the LAN at a stable IP: use
  `hostNetwork: true` (simplest) or a `LoadBalancer`/MetalLB IP, and set that
  IP as `PBX_IP` in the TFTP config + as DHCP option 150.
- Mount `asterisk/` and `tftp/files/` as ConfigMaps (or a PVC) so config
  survives restarts. RTP range `10000-20000/udp` must be exposed.

## Files

```
pbx/Dockerfile          Asterisk + chan-sccp-b build
asterisk/sccp.conf      SCCP devices (by MAC) + lines (extensions)
asterisk/extensions.conf dialplan (_6XXX calls, 555 Home Assistant, 611 Codex, 600/601/602 tests)
asterisk/rtp.conf       RTP port range
tftp/Dockerfile         dnsmasq TFTP-only server
tftp/files/SEP<MAC>.cnf.xml  per-phone provisioning (points phone at PBX_IP)
add-phone.sh            register an additional phone
docker-compose.yml      both services, host networking
voice/                  Codex and Home Assistant AudioSocket bridges, services, and setup
```
