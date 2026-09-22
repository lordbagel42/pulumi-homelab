# Slack huddles → Cisco phone, using Amazon Chime SDK directly

A selfbot watches one Slack channel and listens for direct huddle invitations
to its Slack account, ringing your existing Asterisk phone (`SCCP/6738` by default).
When you answer, it uses your Slack user session to
join the **native huddle's Chime meeting** and carries two-way audio to the handset.

This is an experimental integration with Slack's **private** `rooms.join` API.
Its response shape is based on published, working selfbot implementations,
not an official Slack API contract. The local transport, container and automated
tests have been verified, and incoming channel/DM invitations were tested live
with the Cisco handset. Re-run the acceptance test when deploying elsewhere.
Slack can change this private API or expire
the session credentials.

## How it works

```text
Slack channel history ──poll every 5 seconds──> selfbot ──AMI──> Asterisk rings 6738
                                                                  │ answer
Slack rooms.join ──native meeting + attendee credentials──> Chime SDK JS
                                                                  ⇅ WebRTC
                                                        Native Slack huddle

Cisco 7945G ⇄ SCCP/RTP ⇄ Asterisk ⇄ AudioSocket ⇄ PCM/WebSocket ⇄ Chime SDK JS
```

The `amazon-chime-sdk-js` client handles the meeting connection and media.
Headless Chromium provides its WebRTC and Web Audio runtime, loading only the
local SDK page. It never opens Slack's website or automates Slack controls.
There is no separate AWS meeting, AWS account, Lambda, SIP trunk or PSTN number.
The existing Asterisk PBX handles the Cisco phone's SCCP protocol.

Only one call runs at a time. Each room receives at most one call attempt,
including after restarts. The selfbot does not join until the handset is answered.
Busy, rejected and missed calls are not retried. Huddle end, handset hangup,
SDK failure and the configured time limit close the connection. Audio is streamed
in memory and is not recorded. Channel message text and credentials are not logged.

Incoming invitations show the inviter's Slack display name on the Cisco phone,
falling back to their full name or username. If the profile lookup fails or takes
more than two seconds, the phone still rings as **Slack huddle**. The bridge sends
both caller-ID and connected-party information so SCCP displays the name while
ringing and after answering.

## Deploy on Proxmox

### Outgoing calls and the voice operator

On VM 233, dial **0** for the Slack operator. Say **“call me”** to call your own
Slack account, or say **“call [name or username]”**. It reads back the member's
name and username. Say **“yes”** or press **#** to connect; say **“no”** or press
**\*** to search again. For multiple matches, say or press **1–5**, then confirm.
Wait for the prompt to finish before speaking. Hang up to leave the huddle.

The operator uses the existing voice service's local Whisper/Piper models.
After confirmation, it forwards the current handset audio into the Chime bridge;
it stops transcribing during the huddle. No second phone call is needed.
Once connected, the handset display changes to the Slack contact's name and their
**88xxxx** callback number from Recent Slack huddles. Without Switchboard's contact
directory, the number field shows the Slack member ID. Display lookup or update
failures do not interrupt the huddle audio.

You can also dial an exact Slack member ID from the VM:

```bash
docker exec huddle-phone-huddle-phone-1 python -m huddle_phone lookup 'name or username'
docker exec huddle-phone-huddle-phone-1 python -m huddle_phone dial U0123456789
```

The `dial` command rings the Cisco phone first. Answering creates or joins the
DM huddle and sends an invitation to that member. Missed/busy phone calls send
no invitation. Huddle creation/invitation requests are never automatically
retried; request IDs remain claimed across restarts for seven days.

The local control endpoint uses a separate `OPERATOR_SECRET` (32+ URL-safe
characters), is bound to loopback, and is disabled when the secret is empty.
The homelab deployment stores it as the encrypted Pulumi setting
`huddle-phone:operatorSecret` and provisions the voice service's private
`/var/lib/codex-phone/state/huddle-operator.json`. `huddle-phone:operatorOwnerId`
sets the “call me” destination. The voice service never receives Slack credentials.
For a standalone install, provide `secret`, `http_port` (8099), `audio_port`
(9094), and optionally `owner_id` in `voice/state/huddle-operator.json`, mode 0600.
The voice service listens on loopback port 9095 for extension 0.

### VM setup

Use a Linux **VM** with Docker Engine and Docker Compose, bridged onto the phone's
LAN. Start with 2 vCPUs and 4 GiB RAM. Run Asterisk and this container on the same
VM: the AMI and AudioSocket connections use loopback and host networking.
The existing PBX/TFTP stack needs its usual LAN ports; the bot adds no public
inbound port. Allow outbound HTTPS and the media connectivity required by
[Chime SDK](https://docs.aws.amazon.com/chime-sdk/latest/dg/network-config.html).

Copy this project to the VM. If moving the PBX to a different IP, follow the
[existing PBX instructions](../README.md) to update TFTP/phone provisioning.
Do not start a second PBX on the same addresses or leave both machines serving
the same phone configuration.

From the project root:

```bash
python3 huddle-phone/configure.py
```

This creates `huddle-phone/.env` with a random AMI secret, and the matching
`huddle-phone/generated/manager.conf`. It does not restart anything. The optional
Compose override mounts that manager configuration into the PBX; if you already
maintain your own AMI configuration, merge the generated user and loopback
listener into it and adjust the mount instead of replacing your file.

Edit `.env`:

| Setting | Value |
| --- | --- |
| `SLACK_WORKSPACE` | Workspace subdomain or hostname, e.g. `my-team` or `my-team.enterprise.slack.com` |
| `SLACK_TEAM_ID` | `T...` workspace ID |
| `SLACK_ENTERPRISE_ID` | Enterprise Grid only: `E...` organization ID |
| `SLACK_CHANNEL_ID` | `C...` or `G...` channel ID |
| `SLACK_CLIENT_TOKEN` | `xoxc-...` token from your logged-in Slack web session |
| `SLACK_COOKIE` | `d=xoxd-...` from that **same** session, enclosed in single quotes |
| `PHONE_CHANNEL` | Keep `SCCP/6738` for the existing Cisco phone |

To obtain your own session credentials, log into Slack normally in your browser.
In developer tools, inspect a Slack API request in the Network tab for its
`token` form field, and the `d` cookie in browser storage or that request's
Cookie header. Copy those values **directly into the local `.env` file**.
The user must already belong to the target channel. The workspace and channel
IDs appear in its URL, `app.slack.com/client/T.../C...`.

On Enterprise Grid the URL can contain an `E...` organization ID instead.
Set that as `SLACK_ENTERPRISE_ID`; use the channel's `context_team_id` from
`conversations.info` as `SLACK_TEAM_ID`. The service verifies both the organization
and channel workspace and includes the workspace context in its API requests.

These are account session credentials, not an `xoxb` bot token or an `xoxp`
OAuth token. Keep `.env` private; anyone with both credentials can act as that
Slack user. The phone appears as this user in the huddle. A dedicated user avoids
sharing the same huddle identity with your desktop; the adapter sends
`multidevice=true` as observed in the reference implementation. Workspace access
policies still apply. No Slack app installation is required.

Build, and check the session without joining a huddle or ringing the phone:

```bash
docker compose -f docker-compose.yml -f docker-compose.huddle.yml build huddle-phone
docker compose -f docker-compose.yml -f docker-compose.huddle.yml run --rm huddle-phone doctor
```

Start in the default `DRY_RUN=true` mode:

```bash
docker compose -f docker-compose.yml -f docker-compose.huddle.yml up -d huddle-phone
docker compose -f docker-compose.yml -f docker-compose.huddle.yml logs -f huddle-phone
```

Start a huddle in the configured channel. Within roughly five seconds the log
should show `DRY RUN: would call SCCP/6738`. Huddles older than
`MAX_HUDDLE_AGE_SECONDS` (120 by default) are deliberately ignored. Polling can
miss huddles that start and end between scans, and can be delayed by rate limits.

Set `DRY_RUN=false` and start/recreate the PBX with the optional configuration.
Recreating the PBX interrupts any current phone calls, so do this while idle:

```bash
docker compose -f docker-compose.yml -f docker-compose.huddle.yml up -d pbx tftp huddle-phone
docker compose -f docker-compose.yml -f docker-compose.huddle.yml run --rm huddle-phone doctor
docker exec sccp-pbx asterisk -rx 'dialplan show huddle-phone'
```

`doctor` now checks AMI login as well. The base dialplan uses `#tryinclude` so
the huddle context is loaded only when the override mounts its file.
If changing `AMI_SECRET` or `AMI_PORT`, rerun `configure.py` and recreate the PBX
and bot together. AMI uses only `originate,call` permissions and binds to loopback.

## Live acceptance test

1. End the dry-run huddle and start a **new** huddle in the configured channel.
   The persisted record prevents a previously observed room from ringing again.
2. Answer extension 6738. Allow a few seconds for Slack's private join request
   and the Chime SDK connection. Confirm another huddle participant hears you
   and you hear them on the phone.
3. Hang up. Confirm the connection closes and the selfbot leaves through the
   Chime SDK. Slack's participant display may take time to update.
4. Repeat with a missed/rejected call; the selfbot should never join.
5. Start a huddle in another channel; the phone should stay quiet.

Without your Slack session and a real peer in a huddle, automated tests cannot
establish compatibility of Slack's current private API or prove live two-way audio.
`rooms.join` is channel-based: the service rechecks the room before joining and
rejects credentials for a different room. Slack does not provide a documented
atomic “join only this existing room” guarantee; a room ending at precisely that
moment remains a private-API race to watch for during live testing.

## Operations and troubleshooting

- Health: `curl -f http://127.0.0.1:8099/healthz`. This reports recent successful
  polling and call phase, not a guarantee of active media or handset registration.
- `invalid_auth` / `token_revoked`: refresh the `xoxc` token and `d` cookie from
  the same session, then recreate the bot. Do not paste them into logs or issues.
- `unsupported_media_backend` / `missing_chime_credentials`: Slack changed the
  `call.free_willy.meeting/attendee` contract. The call fails closed. Update
  `huddle_phone/slack.py` against an observed, authorized session response.
- `room_changed_during_join`: the returned room is different; no audio is connected.
- AMI failure: verify the generated file is mounted, secrets match, and AMI is
  reachable on the same VM. `doctor` never places a call.
- Phone rings but no audio: verify `app_audiosocket.so` is loaded, the huddle
  dialplan exists, and outbound Chime traffic is allowed. Logs report separate
  `phone_rx_bytes` and `phone_tx_bytes` when closing; these include silence.
- Chime failures report the initialization stage, error type and numeric SDK
  status without logging credentials or signed URLs. The browser build maps
  Node's `global` reference to `globalThis` for the SDK's CSP monitor. The offline
  browser test initializes a real meeting session and audio input/output before
  holding signaling locally; merely loading the SDK bundle is insufficient.
- Direct invitations use the user's authenticated WebSocket from private
  `client.getWebSocketURL`. Invite the account from a DM or the huddle's invite
  button; this works outside the watched channel. `INCOMING_INVITES=true` is the
  default. Health includes `invitations_connected`; an unavailable feed is
  unhealthy and reconnects with backoff. No Slack UI or public bot app is used.
- Each invitation is deduplicated by its Slack event timestamp. A new invitation
  to the same still-running huddle can ring again after a missed or finished
  call. Old/replayed invites do not ring. Cancelling an unanswered invitation
  stops ringing; answering or accepting elsewhere does not tear down an already
  connected phone call. Only one phone huddle is handled at a time.
- Invited rooms are checked with private `screenhero.rooms.info` and joined by
  their exact room ID. Invitation handling does not fetch DM message contents
  or retain the prejoin credentials included in invitation events.
- If Slack's API cannot be verified during a call, the service closes it.
  `Retry-After` is respected without a request loop.
- The `huddle-state` named volume contains deduplication metadata, not audio or
  join tokens. Keep it on persistent storage. Run **one instance** of this bot,
  including when moving the VM between Proxmox nodes.
- Calls are capped by both the process and Asterisk's absolute timeout. Abrupt
  process death drops the AudioSocket connection; unanswered calls still have
  their ring timeout. Docker restarts the process after failures.

## Local verification

```bash
cd huddle-phone
uv venv --python 3.12 .venv
uv pip sync requirements.txt
npm ci
npm run build
.venv/bin/python -m unittest discover -s tests -v
npm test
```

An offline smoke test loads the bundled SDK in the actual container, creates its
authenticated media socket, and runs real Chromium AudioWorklet PCM generation:

```bash
# From the project root; no network access or credentials are supplied.
docker run --rm --network none --read-only \
  --tmpfs /tmp:mode=1777 \
  --tmpfs /home/bridge:uid=10001,gid=10001,mode=700 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -e PYTHONPATH=/app -v "$PWD/huddle-phone/tests:/tests:ro" \
  --entrypoint python huddle-phone:local /tests/browser_smoke.py
```

Protocol references:
[HQ Fishbowl private join adapter](https://github.com/deployor/hq-fishbowl/blob/c1619f146c71c88474ac047baaa1532f0e0f270a/huddleUtils.js),
[DJ Hakkun user-session adapter](https://github.com/v1ctorio/dj-hakkun/blob/53d94eba15c1f9a9b9c04ae51addbb150edbea88/hakkun-slack-expert/userbot.py),
[Amazon Chime SDK JavaScript](https://github.com/aws/amazon-chime-sdk-js),
[Slack huddle thread object](https://docs.slack.dev/reference/objects/conversation-object/),
[Asterisk AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).
