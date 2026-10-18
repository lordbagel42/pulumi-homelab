# External services: call the handset and stream audio

The call API and its local audio listener start with Switchboard. There is no
feature flag, speech engine, model account, or Slack requirement. Calls go to the
administrator-configured handset, not arbitrary PSTN numbers. The external
service supplies and receives audio; Switchboard does not transcribe or record it.

## PBX connection

Run Switchboard and Asterisk in the same network namespace, as the existing phone
services do. AMI and AudioSocket stay on loopback; expose only the HTTP/WebSocket
service through an authenticated HTTPS reverse proxy or private VPN. Use one
Switchboard worker and one persistent state directory. A process lock rejects a
second worker on that state directory; separate state directories are not a
multi-instance deployment strategy for the same handset.

Supply the existing PBX connection settings to Switchboard:

| Setting | Meaning |
| --- | --- |
| `PHONE_PLATFORM_CALL_CHANNEL` | Required fixed handset, e.g. `SCCP/1000`; falls back to `PHONE_CHANNEL` |
| `PHONE_PLATFORM_AMI_USER` | AMI user; falls back to `AMI_USER`, then `switchboard` |
| `PHONE_PLATFORM_AMI_SECRET` | AMI secret; falls back to `AMI_SECRET` |
| `PHONE_PLATFORM_AMI_PORT` | Loopback AMI port; falls back to `AMI_PORT`, then `5038` |
| `PHONE_PLATFORM_CALL_TOKEN` | Optional externally provisioned 32+ character call-only token |
| `PHONE_PLATFORM_CALL_AUDIO_PORT` | Optional fixed loopback audio port; default `0` allocates one automatically |
| `PHONE_PLATFORM_CALL_INCOMING_PORT` | Loopback FastAGI port; provisioner sets `4573`, local default `0` allocates one |

Store secrets in the service's private environment/credential mechanism, not Git.
Give the AMI account `read=call` and `write=originate,call`; restrict it to loopback.
It must permit Login, Getvar (`DEVICE_STATE`), Originate and Hangup. Load the
existing AudioSocket application and include `deploy/stream-calls.conf` at the
top level of the PBX's `extensions.conf`. Its `switchboard-stream` context must
not be included in a handset-facing context. The dialplan sets an absolute call
limit independently of the API process. Coordinate any PBX reload with its owner;
adding these source files does not change a running PBX.

The Proxmox platform provisioner takes a `calls` object with `channel`, `user`,
and `secret` in its private JSON input. It writes only those AMI settings to
mode-0600 `calls.env`, which the service loads; no Slack credential is needed.
After the existing idle check, it installs `stream-calls.conf` in the mounted
Switchboard directory and loads it through a top-level include in `routes.conf`.
Existing aliases are retained, including after later dashboard edits. This needs
only a dialplan reload and platform restart, not a PBX/container rebuild.

Missing handset/AMI configuration returns HTTP 503 from call creation, while the
API and listener remain available. A non-idle or unknown device state refuses to
dial. The busy check is not an atomic reservation shared with other phone
services: retain the PBX's device-level call limits and do not originate the same
handset concurrently through another service.

## Credentials and endpoints

By default Switchboard creates `call-token` in `PHONE_PLATFORM_STATE_DIR` with
mode 0600. Give that token to the external service; it can use only `/api/v1/calls`
and its child routes. It cannot read sessions, change settings, or sign in to the
dashboard. The admin token also works, but is unnecessary. All clients sharing
the call token share control of the one handset; this is not multi-tenant auth.

External clients send `Authorization: Bearer <token>` on HTTP and WebSocket
connections. Existing admin dashboard sessions can also manage calls over HTTP.
WebSockets do not accept cookies, URL tokens, or browser `Origin` headers. Use a
server-side WebSocket library supporting authorization headers. Require HTTPS/WSS
outside the local host; never expose AMI or the raw AudioSocket port publicly.
Configure your reverse proxy to support WebSocket upgrades and call-length idle
timeouts, and to redact authorization headers. The media path is relative so it
works with the externally configured HTTPS origin without trusting proxy headers.

| Operation | Endpoint |
| --- | --- |
| Reserve a call | `POST /api/v1/calls` with `Idempotency-Key: <UUID>` |
| Poll incoming handset offers | `GET /api/v1/calls/incoming` |
| Inspect it | `GET /api/v1/calls/{id}` |
| Hang up or cancel ringing | `DELETE /api/v1/calls/{id}` |
| Stream two-way audio | WebSocket `/api/v1/calls/{id}/media` |

Creation accepts `{"max_duration_seconds":600}`; the default is 600, valid range
1–3600. Other fields are rejected. It returns HTTP 201 and a record like:

```json
{
  "id": "<server-generated UUID>",
  "direction": "outbound",
  "state": "awaiting_media",
  "reason": null,
  "created": 1791700000.0,
  "updated": 1791700000.0,
  "max_duration_seconds": 600,
  "media_path": "/api/v1/calls/<server-generated UUID>/media",
  "audio": {
    "encoding": "pcm_s16le",
    "sample_rate": 8000,
    "channels": 1,
    "frame_bytes": 320
  }
}
```

Connect the WebSocket within 15 seconds. **Connecting starts ringing**, so an
absent audio service never leaves the handset ringing with nobody listening.
There is one call reservation and one WebSocket per call. Reconnection is not
supported: disconnecting hangs up. Calls respect the existing quiet-hours policy,
both at reservation and before ringing. They are rejected, not queued for later.

Persist the request UUID before submitting. After a lost HTTP response, POST the
same key and body to recover the same call, even after a restart. A different body
with the same key returns 409. A new key represents a new deliberate phone call;
never automatically redial with a new key. Restarted in-flight records become
`interrupted` and cannot be replayed. Call metadata and keys persist in SQLite;
audio does not. Do not prune request records while clients could still retry them.

## Dial 333 to reach June

The handset's 333 route enters a loopback FastAGI gate, **without answering**.
June polls `GET /api/v1/calls/incoming` once per second with the same call-only
bearer credential. It returns `{"calls":[]}` or one call record with
`direction: "inbound"`, `state: "offered"`, an `expires_at` Unix timestamp, and
the existing UUID `id`, `media_path`, and `audio` fields. Repeated polls return
the same ID; reading does not consume or answer the offer. The offer expires
after 30 seconds and an accepted conversation is limited to 600 seconds.

The inbox stays empty and media attachment is refused until AMI login succeeds;
the unanswered handset still occupies the single-call slot during control setup.
This ensures rejecting a published offer does not cancel its own AMI login and
lose the connection needed for exact-channel cleanup. Slow login does not extend
the original absolute expiry. Later AMI permission or connection failures still
produce `interrupted` / `hangup_unconfirmed`, not a successful rejection.

Prepare the conversational speech session first. **Attaching the authenticated
media WebSocket explicitly accepts the incoming call**; there is no accept POST.
Only one consumer can attach, and reconnecting is not supported. The lifecycle
is `offered` → `answering` → `connected` → `ending` → a terminal state. Wait for
`connected` before sending PCM. GET and DELETE by ID work as for outbound calls;
DELETE rejects an unaccepted offer. Caller hangup, rejected authentication,
service shutdown, unavailable June, and offer expiry do not answer the handset.
An AMI/control failure still tears down the gate and reports uncertainty when
the exact-channel hangup cannot be confirmed. Never recreate/retry a call under
a new ID after uncertainty.

Inbound calls share the outbound single-call slot, but bypass outbound quiet
hours because they are deliberate actions by the handset user. Only the fixed
configured handset is admitted; AMI/PBX channel identities are not exposed in
the inbox. Historical outbound descriptions also include `direction: "outbound"`.
Audio transport is unchanged and no speech model or recording lives here.

Provisioning installs `deploy/incoming-calls.conf` alongside `stream-calls.conf`,
retains both includes during alias edits, and reserves extension 333. An existing
333 alias aborts provisioning rather than being silently replaced. The FastAGI
listener is loopback-only and uses existing AMI permissions. Its dialplan clears
all gate variables, sets `AGIEXITONHANGUP=yes`, and has a 40-second independent
watchdog. `AGISTATUS=SUCCESS` alone cannot answer: the gate's final acceptance
marker is also required before entering the existing Answer/AudioSocket context.
No PBX rebuild or restart is needed, only the guarded reload/platform restart.

## Wire format

- Text messages from Switchboard are JSON. `{"type":"state","state":"connected",...}`
  means the handset answered and its audio socket is attached. Other states are
  `awaiting_media`, `dialing`, `offered`, `answering`, `ending`, `ended`, `failed`,
  `expired`, `interrupted`.
- `{"type":"dtmf","digit":"5"}` reports a handset key. The bundled PBX's
  AudioSocket DTMF patch is required for these messages.
- Binary messages in **both directions** are exactly 320 bytes: 160 signed 16-bit
  little-endian samples at 8,000 Hz, one channel, 20 ms. No WAV header, base64,
  JSON wrapping, Opus or μ-law. Resample other formats in the external service.
- Wait for `connected`, then send one frame every 20 ms. Switchboard paces playback
  and inserts silence when no frame is available. It discards pre-answer audio.
- Keep reading while sending. Outbound playback buffers at most 500 ms; the
  receive queue holds at most 50 messages. Overflow or a stalled consumer ends
  the call rather than accumulating unlimited latency/memory.
- Closing the WebSocket or DELETE requests hangup of this call's exact AMI channel.
  Asterisk EOF, malformed media, ring timeout, a five-second phone-media stall,
  and the duration limit also end the call. GET is authoritative if a connection
  closes before the final state event arrives.

Cancellation stays `ending` while a delayed AMI channel announcement is pending
(up to the remaining 30-second ring window plus cleanup time). A lost PBX
connection or unconfirmed hangup reports `interrupted` / `hangup_unconfirmed`,
not a confirmed hangup. Inspect the PBX before deliberately creating another call.
The ring timeout and PBX absolute limit still apply if Switchboard crashes.

If state persistence fails, Switchboard still hangs up and releases the handset,
logs the error type without secrets, and sends `interrupted` / `storage_error`
on the WebSocket. Persisted GET status can be stale until storage recovers; do not
treat it as confirmation that a phone call is still connected during disk errors.

## Executor integration after deployment

`GET /api/v1/calls/openapi.json`, authenticated with the call-only token, describes
only call reservation, incoming offers, status, and hangup. It is generated from
the routes, includes the required `Idempotency-Key` header and bearer security
scheme, and excludes all administrative operations. Reading it never rings the
handset.

After the real HTTPS endpoint is deployed and reachable from executor.sh:

1. Create an Executor provider for that exact host with a secret `token` field;
   connect the generated call-only token through Executor's secure account form,
   never source code, chat, or tool arguments. Use the authenticated schema read
   as the account health check, with an unauthenticated 401 control.
2. Use `liveOpenapiRouter` from `apps/openapi` with the authenticated document
   loaded through `cache.forAccount(account)`. Set `allowedOrigin` and `baseUrl`
   to the deployed origin and bind the `CallToken` scheme to the account's token.
   Do not point the public `source.url` loader at this private schema: Executor
   fetches `source.url` without account credentials.
3. Expose `reserveCall`, `getCall`, and `endCall`. Persist each caller-generated
   UUID before reservation; never turn a timeout into a new request ID. Coordinate
   the audio service before reservation so it can attach within 15 seconds.
4. Verify discovery and authenticated reads first. Only place a real test call
   with authorization for the handset. Add call-control approval policy at the
   Executor app boundary without introducing a Switchboard feature flag.

Executor's bounded tool requests are the control path, not the audio transport.
The external audio service connects directly to the returned media path over WSS
with its securely provisioned token. Installing only the Executor tools does not
create a speech agent, connect a microphone, or consume the call's audio. Do not
send PCM frames through tool results or try to keep a Code Mode execution open
for the duration of a call.

The Executor account/app have not been created by this source change. Registration
awaits a deployed origin and the secure credential connection. These steps follow
Executor's connected `app-authoring/integrations.md` and `code-mode` instructions.

## Python client outline

Use the project's `httpx` and `websockets` dependencies, or equivalent libraries
in your service. Supply `base_url`, `token`, and a previously persisted
`request_id`. This example receives handset audio and sends silence; replace the
silence producer with your service's audio, and consume binary messages as PCM.

```python
import asyncio
import contextlib
import json

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


async def phone_call(base_url, token, request_id):
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=45) as api:
        response = await api.post(
            "/api/v1/calls",
            headers={"Idempotency-Key": request_id},
            json={"max_duration_seconds": 60},
        )
        response.raise_for_status()
        call = response.json()
        if call["state"] != "awaiting_media":
            return call  # Inspect an existing attempt; never redial automatically.
        ws_base = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        ready = asyncio.Event()
        try:
            async with connect(ws_base.rstrip("/") + call["media_path"],
                               additional_headers=headers, max_size=4096) as ws:
                async def transmit():
                    await ready.wait()
                    while True:
                        await ws.send(bytes(320))  # Replace with your next PCM frame.
                        await asyncio.sleep(0.02)

                sender = asyncio.create_task(transmit())
                try:
                    async for message in ws:
                        if isinstance(message, bytes):
                            pass  # Consume the handset's PCM here; do not log it.
                        else:
                            event = json.loads(message)
                            if event.get("state") == "connected":
                                ready.set()
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                        await sender
        finally:
            # Use the same call ID even if the WebSocket handshake failed.
            result = await api.delete("/api/v1/calls/" + call["id"])
            result.raise_for_status()
        return result.json()
```

## Verification boundary

The automated tests use real local AMI and AudioSocket TCP peers and the HTTP/WS
API, but the PBX peer is a simulator. They check distinct sample bytes in both
directions, authentication, duplicate connections/requests, cancellation, delayed
channels, lost acknowledgements, busy state, quiet hours, timeout and restart.
They do not establish SCCP handset registration, LAN reachability, the installed
PBX dialplan, or end-user audio quality. Test an authorized handset call after
deploying the code and PBX context; no live call is made by the test suite.
