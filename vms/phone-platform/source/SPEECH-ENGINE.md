# ElevenLabs Speech Engine

Switchboard can carry a continuous handset conversation through ElevenLabs
Speech Engine while Amp owns the durable task. The existing 611 and 611XXX
callback numbers, saved `codex:<number>` identifiers, pending questions and
history remain compatible. New calls use the configured default agent engine.

The integration uses the official `elevenlabs==2.68.0` SDK to create/update
Speech Engine resources, browse voices, preview speech and obtain private
conversation URLs. An authenticated upstream WebSocket binds each provider
conversation to the session created by the handset call. It streams task
progress, answers and questions without granting transcripts control over which
session to access. Reconnected transcript events are deduplicated; interrupting
playback or hanging up does not cancel an ordinary Amp task.

Speech Engine owns recognition, synthesis, turn detection and interruptions.
The existing Whisper/Piper and one-shot ElevenLabs STT/TTS endpoints remain
available to routes configured with `speech_mode: "legacy"`.

## Configuration

In Integrations, save the ElevenLabs Speech Engine API key, default voice/model,
language and a public `wss://HOST/speech-engine/upstream` URL. Saving the settings
does not create a cloud resource. The resource provision action creates or
updates the saved engine; ambiguous creation leaves a pending marker to prevent
duplicate billable resources. Reconcile such a result with ElevenLabs and save
the actual `seng_...` ID before trying again.

Each agent or Slack operator route can have its own voice, engine, greeting,
personality, speed, stability and supported SDK turn/recognition options.
Blank voice/model/language fields inherit the integration defaults. Distinct
route resources prevent an update to one operator from changing the other.
Personalities reach the agent input; answers to pending questions stay literal.

`POST /api/v1/speech/engine/provision/{route}` provisions the saved route settings.
`GET /api/v1/speech/engine/voices` lists voices.
`POST /api/v1/speech/engine/preview/{route}` accepts `{"text":"Hello"}` and returns
WAV audio using the saved voice. These endpoints require the normal Switchboard
admin authentication. Voice previews and conversations consume ElevenLabs credits.
Known quota errors are surfaced as exhausted credits without exposing provider
payloads or keys.

## Deployment

1. Install `requirements.txt` in the platform runtime. Python 3.13 and newer use
   the pinned `audioop-lts` compatibility package for PCM conversion; the phone
   VM's Python 3.11 uses its standard `audioop` module.
2. Run one Switchboard worker. Expose only `/speech-engine/upstream` through the
   public TLS proxy, preserving WebSocket upgrades and the
   `X-Elevenlabs-Speech-Engine-Authorization` header. Keep the dashboard and admin
   API private. Incoming provider JWTs are verified before accepting the socket.
3. Configure the cloud resource and saved route profiles. The helper
   `deploy/configure-speech-engine.py --upstream-url wss://HOST/speech-engine/upstream`
   reuses credentials already stored on the VM. It leaves legacy audio selected
   unless `--activate` is supplied. Do not retry uncertain creation blindly.
4. Deploy `deploy/phone-platform.service` after the existing idle check. It
   enables the loopback AudioSocket listener on 9096 for the deployed service;
   confirm the port is free before starting it. Local development retains port 0
   by default. The listener permits only configured PBX peers and one handset
   call at a time. The optional `deploy/speech-engine.conf` drop-in has equivalent
   settings for an older installed service unit.
5. Deploy the huddle/PBX source snapshot after the platform listener is ready.
   `asterisk/extensions.conf` already includes the reviewed changes described by
   `deploy/speech-engine-dialplan.patch`; do not apply the patch a second time.
   It directs 611, callback extensions and the Slack operator to 9096; legacy
   routes relay to their existing listeners (9092 and 9095), preserving their
   UUID handshake. Recent-contact directory calls always use the existing Slack
   operator. Changing a route to `speech_mode: "speech-engine"` selects cloud
   media on its next call, without a service restart.
6. Verify the handset conversation, callback, pending answer, interruption,
   hangup/resume and any requested Slack handoff. Account credits and a working
   public TLS upstream are prerequisites for live cloud verification.

Revert a route to `speech_mode: "legacy"` for local speech without a service
restart. If Switchboard itself is unavailable, restore the old PBX destinations
through the infrastructure source. Preserve the session store and credentials.

The `*` key stops playback; `**` requests task cancellation. `0` repeats the
question or saved reply, and digits select pending choices. Speech Engine owns
turn detection, so `#` does not commit another turn. Slack operator sessions
retain their existing call-scoped lifetime and handoff behavior.

## Source and verification

This imports the prior undeployed Proxmox implementation from
`192.168.0.233:/var/lib/codex-phone/workspace/switchboard`, inspected on
2026-09-21. That workspace contained no Git metadata. SHA-256 digests before
the Amp migration and current integration fixes:

| Source file | SHA-256 |
| --- | --- |
| `phone_platform/speech_engine.py` | `7cf9784a846b61bd376735ef5a5a92a066b04a32187e9bc44ca6918d615ac015` |
| `phone_platform/speech_engine_media.py` | `136b7a671aeec999f3699184fdfcb8632202eede4b6c37a362a0bab1d11888bd` |
| `phone_platform/operator_profiles.py` | `86917feb61a9fd71200aea046e8be818b68e435b67a5b2352b2af2490b959033` |

Run `PYTHONPATH=. .venv/bin/python -m pytest tests/test_speech_engine.py tests/test_operator_profiles.py`.
The tests use mock cloud transports and simulated handset/upstream sockets for
SDK contracts, JWT rejection, resource separation, replay suppression, durable
submission ordering, audio conversion, interruption, quota failures, Amp session
creation, saved voice previews and Slack handoff. They make no paid API calls.
The separate one-shot speech tests remain in `tests/test_speech.py`.

References: [Python Speech Engine SDK](https://elevenlabs.io/docs/eleven-api/resources/libraries/speech-engine/python-sdk-reference),
[upstream authentication and protocol](https://elevenlabs.io/docs/api-reference/speech-engine/speech-engine-upstream),
[resource creation](https://elevenlabs.io/docs/api-reference/speech-engine/create),
[client events](https://elevenlabs.io/docs/eleven-agents/customization/events/client-events).
