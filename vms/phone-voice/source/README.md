# Talk to Amp from the Cisco phone

New conversations use the `homelab-amp` runner, with native Amp thread links
visible in Switchboard. The phone VM needs its own Amp login and the runner
needs the phone plugin; see [Amp setup](AMP.md). Existing Codex conversations
keep their engine, history, and callback numbers. The service and socket retain
their original names so existing callbacks and clients continue to work.

The [ElevenLabs Speech Engine](../phone-platform/SPEECH-ENGINE.md) integration
provides continuous cloud media when enabled and provisioned. Local Whisper/Piper
remain the recovery path. A failed cloud synthesis request falls back to Piper,
and playback stays alive after individual utterance failures.

[Switchboard](../phone-platform/README.md) is the shared dashboard and API at
`http://192.168.0.233:8088`. It lists these sessions, handles API registrations and
callbacks, provides shared speech, and exposes resumable sessions in the phone's
Directories menu. The existing session database remains the source of callback
numbers and history.

The workstation's CLI/MCP client reads its private `voice/state/platform.json`
registration and connects to Switchboard. Server-side voice services use
`PHONE_PLATFORM_URL` and `PHONE_PLATFORM_TOKEN_FILE`. Without central configuration,
the original private Unix socket transport remains available for recovery.

The production phone services now run on **Proxmox VM 233, 192.168.0.233**.
Dial **0** for the Slack huddle operator. Say “call me” or a member's name,
then talk naturally with its agent operator. It asks when a name or request
needs clarification; answer in your own words or select a numbered option.
Say “cancel” or press **\*** to end the operator call.
The existing call becomes the huddle; hang up to leave it. See the
[huddle guide](../huddle-phone/README.md#outgoing-calls-and-the-voice-operator)
for dialing exact Slack IDs from the command line.

Press the handset's **Directories** button to browse application directories.
Choose **Amp sessions**, **Codex sessions**, **Recent Slack huddles**, or another registered app,
select an entry, and press **Dial**. Recent huddles appear after a call connects.
When the operator connects your handset to a huddle, the phone updates its display
to the contact's Slack name and their **88xxxx** directory callback number. The
update targets that specific phone call, so a delayed response cannot change the
display of a later call.

New phone-created tasks run on the configured Amp runner in
`/home/amp/workspaces`. The runner continues working when the caller hangs up.
The saved session records its executor, so changing the configured runner cannot
silently move an existing conversation. A disconnected stream is reported as an
uncertain remote task, not a successful cancellation or an automatic retry.

Legacy Codex tasks can still run on Proxmox or the workstation. Proxmox Codex
workspaces use `/var/lib/codex-phone/workspace`; workstation conversations run
as `raygen` in their original local project. Their saved host remains unchanged.

Each saved callback number stays on the host originally selected. Existing
migrated sessions remain on Proxmox. The workstation's `codex-phone-tunnel.service`
privately forwards its Codex app-server to the VM and retains the original
control socket for recovery. Registered CLI/MCP clients use the central API.
The `codex-phone-workstation.service` owns that local app-server.
The original local voice, PBX and TFTP services are disabled and retained for rollback.
Deployment and recovery are documented in the homelab repository's
`vms/phone-voice/README.md`. The VM uses Whisper `base.en` for response time;
the original `small.en` model remains cached.

Lift the handset and dial **611** to create a persistent Amp session.
It announces a callback number such as **611-005**. Dial that as **611005**
(six digits, no hyphen) to return to the same conversation later. Enter the
full number before lifting the handset, or press Dial after entering 611;
the PBX also waits for more digits when 611 is a prefix of a longer number.

Describe a task, pause for about a second, and hang up whenever you want.
The task keeps running on the chosen host. On a callback, you hear the latest progress, result,
or pending question. Additional instructions steer a running task. The
Proxmox VM and bridge service must remain running for phone access. Workstation
sessions also require this workstation and its private tunnel. If their connection
is lost, the bridge reports that the task may still be running; it never moves
or replays the task on the other host. Reconnect and call its saved number to continue.

You can **talk over a spoken reply** to interrupt playback.

Spoken interruption uses a streaming Silero speech classifier requiring
120 ms of confident speech within a 200 ms window, plus echo rejection. It is deliberately
stricter than the original WebRTC-only detector, which the live handset test
found could react to breathing. A 1.2-second pause ends an utterance.

Keypad controls:

| Input | Action |
| --- | --- |
| `*` | Stop speech immediately; keep the task running |
| `**` within 1.5 seconds | Stop the running task; preserve its conversation |
| “stop the task” / “stop working” | Stop the running task |
| “stop talking” | Stop speech only |
| `0` | Read this session's number and latest status |
| `#` | Submit the current utterance without waiting for silence |
| “hang up” / “end the call” | End the phone connection; keep the task running |

Calls are limited to 30 minutes and one simultaneous caller. Tasks have no
call-duration or three-minute timeout. Session numbers are never recycled;
the current numbering scheme supports 001 through 999.

The desk phone is extension **6738**, MAC `0C2724317F2E`.
To ring it once and connect its answer to a phone session:

```bash
voice/.venv/bin/python voice/call_phone.py
voice/.venv/bin/python voice/call_phone.py 611005  # Call an existing session
```

This rings for at most 45 seconds and does not retry an unanswered call.

## Legacy Codex transport

The remaining Codex-specific transport and app-server details document existing
sessions. New runner sessions use the protocol and plugin described in [AMP.md](AMP.md).

```text
Cisco 7945G --SCCP/RTP--> Asterisk --AudioSocket--> Python bridge
                                                      |
                                      Switchboard speech recognition
                                                      |
                                      authenticated Codex app-server
                                                      |
                                       Switchboard speech synthesis
                                                      |
                                                   handset
```

The bridge listens only on `127.0.0.1:9092`. Asterisk handles the phone's
network audio and provides 8 kHz PCM to the bridge. Switchboard provides shared
speech services, defaulting to local Whisper `base.en` recognition and Piper
`en_US-lessac-medium` synthesis. Choose different defaults or per-route providers
in the dashboard. Speech transcripts go to Codex through the installed
CLI's existing ChatGPT login; no separate OpenAI API key is configured.

Each session has a saved Codex conversation in its configured workspace,
with full filesystem/network access (`danger-full-access`), no approval prompts,
live web search, available agent tools, and low
reasoning effort for shorter response times. It inherits the configured
Codex model (currently `gpt-6-astra`). It can use workspace tools, but it
does not share the desktop conversation that installed this bridge. Multiple
sessions can work independently even though only one handset call is active.

`state/sessions.sqlite3` stores the stable extension mapping, work queue,
latest progress/results, questions, and caller answers. Codex itself
stores full conversation history. Reconnecting never interrupts a turn.
After a bridge/computer restart, the mapping and history survive, but an
in-progress task is marked interrupted. Tell the session to continue;
the bridge does not silently replay commands that might already have run.

The most recent conversation from the original bridge was preserved as
**611001**. Verification sessions also occupy numbers; new sessions receive
the next available number.

## Questions and callbacks

For sessions managed by this bridge, normal Codex `request_user_input`
events are routed to the phone. A `phone_ask_user` tool provides the same
route when the normal input tool is unavailable. If you are connected to
that session, it asks on the existing call. Otherwise it queues a call to
your desk phone, waiting while the phone is busy. Each question rings once
for at most 45 seconds. A missed call stays pending at that session's
callback number; it is never treated as an answer or permission.

Speak naturally or press an offered option number. Your response goes to
Codex, which is instructed to briefly restate the meaning and respond
conversationally. You can correct it, ask a follow-up question, or say things
like “That's right. Yes.” It checks its interpretation when meaning or
authorization is unclear; there is no fixed yes/no menu around every answer.
Hanging up without answering leaves the question pending.
No arbitrary outbound telephone number is accepted.

## MCP for other Codex sessions

The `codex_phone` stdio MCP is installed globally in `~/.codex/config.toml`.
New/reloaded Codex clients discover these tools:

| Tool | Purpose |
| --- | --- |
| `ask_user` | Ask a clarification question through the desk phone |
| `get_answer` | Collect a pending answer without ringing again |
| `update_session` | Publish progress/results and obtain a callback number |
| `check_messages` | Collect instructions left during a callback, including stop requests |
| `list_sessions` | List session numbers and status |

The owner preference is recorded in `~/.codex/AGENTS.md`, so sessions know
the phone is available for clarification. Existing clients may need a new
session or MCP reload to discover the newly installed server.

For an external desktop session, the callback number is a status/question
mailbox. Its original host keeps running the task. It should publish
updates and poll `check_messages` between work steps. This bridge cannot
forcibly interrupt or keep alive an unrelated desktop process, and it does
not intercept that host's built-in question UI. Native automatic question
routing and direct turn interruption apply to bridge-managed sessions.

Registered MCP clients connect to Switchboard's authenticated LAN API using their
private client configuration. The original owner-only Unix socket remains a
fallback when central configuration is absent. Question timeouts return
`pending`; they do not delete the question or fabricate an answer.

Audio is processed in memory. Transcripts and answers appear in the user's
systemd journal and Codex's normal session storage. `state/status.json`
contains the current call state and most recent exchanged text; it is
owner-readable only. It also identifies the Codex thread for that call.

## Service

The production system services are enabled on VM 233 and start at boot.
Run these commands on that VM:

```bash
systemctl status codex-phone-bridge.service
journalctl -u codex-phone-bridge.service -f
```

Production units and provisioning are in the homelab repository's
`vms/phone-voice/` module. Its Python environment is `/opt/phone-voice/venv`;
models, sessions and credentials live under `/var/lib/codex-phone`.
The original `voice/codex-phone-bridge.service` and local `voice/.venv` are
retained for development and rollback. Python **3.11 or 3.12** is required
because this implementation uses its built-in `audioop` resampler.
Dependencies are pinned in `requirements.txt`.

On this workstation, check the private CLI/MCP tunnel with:

```bash
systemctl --user status codex-phone-tunnel.service
systemctl --user status codex-phone-workstation.service
```

Local controls, using the same private API:

```bash
voice/.venv/bin/python voice/phone_cli.py sessions
voice/.venv/bin/python voice/phone_cli.py create --host workstation --cwd /home/raygen/homelab/pulumi-homelab
voice/.venv/bin/python voice/phone_cli.py session --session-id 611005
voice/.venv/bin/python voice/phone_cli.py interrupt --session-id 611005
```

`bridge.py` is the service entry point; `phone_bridge.py` owns detachable
audio, `session_manager.py` owns tasks, and `session_store.py` owns durable
state. `audio.py` is shared with the separate Home Assistant phone service.
Restarting the bridge interrupts active work, so use the session controls
for individual task cancellation instead of restarting the service.

Asterisk 20.6's AudioSocket application originally discarded keypad events.
`pbx/app_audiosocket.c` is the GPL upstream 20.6 source with a small DTMF
forwarding backport (protocol type `0x03`); its original license is retained.
The Dockerfile builds this module against the image's Asterisk headers.

The PBX and TFTP containers run on VM 233. To inspect the PBX there:

```bash
docker exec sccp-pbx asterisk -rx 'sccp show devices'
docker exec sccp-pbx asterisk -rx 'dialplan show 611@sccp-internal'
```

The handset uses Alternate TFTP `192.168.0.233`; its download and registration
were verified after the change. The single-phone recovery DHCP helper is
available as `cisco-phone-dhcp.service` on VM 233 if needed. See the homelab
module's recovery instructions before restarting any old workstation containers.

During the live test at 23:44:58 MDT on September 19, a hold/device-disconnect
sequence exposed a crash in the existing `chan_sccp` driver, at
`sccp_refcount_find_obj` in `sccp_refcount.c:462`. Docker restarted Asterisk and
the handset registered again at 23:46:50. The saved Codex sessions survived.
That driver crash remains a known PBX issue; use hangup and the saved callback
number to leave and return to a task.

## Verification

With no live call in progress, this integration test sends a synthesized
question through the real bridge, checks Codex's answer, and transcribes
the returned audio to confirm the answer is audible:

```bash
voice/.venv/bin/python voice/smoke_test.py
```

The test passed with the spoken question “What is two plus two?” and the
returned spoken answer “Two plus two is four.” Asterisk's `611` route is
also checked separately with a local test channel before ringing the phone.

The live handset call was answered at 23:10 MDT on September 19, 2026.
The bridge recognized the caller, returned Codex's spoken answer, and the
caller confirmed the audio was understandable. Two-way speech was verified
over the phone's actual SCCP/ulaw RTP connection.

Persistent-session verification also passed:

- Isolated session tests cover stable numbers, hangup, steering, explicit
  cancellation, conversational answers, missed calls, callback queuing, and restart behavior.
- Audio regression tests reject loud breath-like noise while accepting actual
  synthesized speech, including quiet G.711 audio; follow-up corrections reach
  the same Codex conversation. Stopping work discards older speech still being transcribed.
- Real Codex native and dynamic question requests received correctly formatted
  answers in an isolated test database, without calling the handset.
- A real shell task finished after AudioSocket hangup; reconnecting announced
  the saved result from the same Codex thread.
- Synthesized speech interrupted playback; `*` stopped playback; `**`
  interrupted an actual Codex turn while retaining its history.
- A real stdio MCP question traveled through the bridge and returned its
  keypad answer without a second confirmation menu.
- A complete synthesized conversation answered a question about blue and white
  colors, then accepted a spoken correction to dark green in the same thread.

```bash
PYTHONPATH=voice voice/.venv/bin/python -m unittest discover -s voice/tests -v
PYTHONPATH=voice voice/.venv/bin/python voice/tests/live_protocol.py
PYTHONPATH=voice voice/.venv/bin/python voice/tests/live_bridge.py
PYTHONPATH=voice voice/.venv/bin/python voice/tests/live_conversation.py
```

The live tests use the authenticated Codex account and create clearly named
verification sessions. Run them only when there is no real Codex phone call.

References: [Codex app-server](https://developers.openai.com/codex/app-server/),
[Asterisk AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/),
[Asterisk call files](https://docs.asterisk.org/Configuration/Interfaces/Asterisk-Call-Files/),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), and
[Piper](https://github.com/OHF-Voice/piper1-gpl).
