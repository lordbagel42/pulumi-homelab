# Switchboard

A shared dashboard and API for the Cisco phone. The central service lives on
the phone's Proxmox VM at **http://192.168.0.233:8088**. The browser dashboard,
Amp runners, compatible Codex calling tools, voice services and application
adapters use the same API. Dial **611** to start an Amp conversation.

Switchboard provides:

- Amp sessions with native thread links, executor details, stable callback
  numbers, progress, questions, caller answers, steering and cancellation.
  Existing Codex sessions retain their engine and original host.
- Reusable Amp operators: create a session with a task and instructions, send
  more input, read progress, answer questions, and request a handset callback.
- Slack member lookup and native huddle controls, including an Amp phone
  operator and direct dialing from recent contacts.
- Shared Whisper recognition and Piper synthesis, configurable ElevenLabs
  STT and TTS, and the ElevenLabs Speech Engine SDK for continuous conversation.
  Cloud providers stay disabled until configured.
- Per-route speech providers and additional dial numbers. Native 555, 611 and 0
  remain available; aliases cannot replace reserved phone numbers.
- Optional app directories on both the dashboard and Cisco handset. Separate
  Amp and Codex directories list callback sessions; Slack lists contacts after a huddle actually connects.
  Other apps can publish their own entries. Home Assistant does not register a
  directory.
- Ordered event history, JSON APIs, an OpenAPI schema, and a small CLI for tools.

## Sign in

The API requires a bearer token. The dashboard exchanges a token for an HttpOnly,
SameSite session cookie; it does not keep credentials in browser storage. To
generate a single-use browser link, run on the phone VM:

```sh
cd /opt/phone-platform/current
runuser -u codex-phone -- env PHONE_PLATFORM_STATE_DIR=/var/lib/codex-phone/switchboard \
  /opt/phone-platform/venv/bin/python -m phone_platform login-link
```

The link expires after 15 minutes. Browser sessions expire after 12 hours. The
private API credential is `/var/lib/codex-phone/switchboard/admin-token`. The
handset uses a different credential that grants access only to directory XML.
Credentials and configuration are outside application releases. Amp uses its
configured runner connection. Legacy Codex sessions use their existing Codex
login. ElevenLabs requires its own API key, entered under **Integrations**.

## Build a feature

`/api/docs` lists the API and `/api/openapi.json` describes request schemas.
Most integrations need only a few calls. All examples below require
`Authorization: Bearer <private API token>` and JSON content type.

Create a managed operator with `POST /api/v1/operators`:

```json
{
  "title": "My feature's operator",
  "prompt": "Help the owner choose the next step.",
  "instructions": "Keep spoken replies concise. Ask when a choice is needed.",
  "host": "proxmox",
  "engine": "amp",
  "integration": "my-feature"
}
```

The returned session keeps a compatible ID such as `codex:23` and a callback
extension such as `611023`. The ID prefix is stable across engines; use `engine`
to identify Amp or Codex. Amp sessions also report `executor`, native `thread_id`
and `thread_url`, which the dashboard links to for reviewing the full Amp thread.
New operators default to Amp, including operators for custom integrations.
Explicit `engine: "codex"` selects the legacy backend. Supply an
`Idempotency-Key` header when creating operators to prevent
duplicate tasks after a lost response. An uncertain request must be inspected,
not submitted again with a new key.

| Operation | API |
| --- | --- |
| Read session and pending questions | `GET /api/v1/sessions/{id}` |
| Send instructions | `POST /api/v1/sessions/{id}/input` with `{"text":"..."}` |
| Stop work | `POST /api/v1/sessions/{id}/cancel` |
| Request a callback | `POST /api/v1/sessions/{id}/callback` |
| Queue a spoken question | Same callback endpoint with `{"question":"...","options":["A","B"]}` |
| Answer a question | `POST /api/v1/questions/{id}/answer` with `{"item_id":"answer","text":"..."}` |
| Read events | `GET /api/v1/events?after=123` |
| Subscribe to events | `GET /api/v1/events/stream?after=123` |

An externally managed tool registers using `POST /api/v1/sessions`:

```json
{
  "title": "Build task in another app",
  "integration": "my-feature",
  "external_key": "a-stable-task-id",
  "metadata": {"project": "example"}
}
```

Reusing the integration and external key returns the same phone session. Publish
progress using `PATCH /api/v1/sessions/{id}` with `summary` and `state`. The
original tool remains responsible for the task; Switchboard owns phone access.
The existing Codex MCP tool uses the compatibility `/api/v1/phone/rpc` endpoint.

## Optional directories

Register once with `POST /api/v1/directories`:

```json
{"id":"my-app","name":"My application","integration":"my-feature","enabled":true}
```

Publish entries with `PUT /api/v1/directories/my-app/entries`:

```json
{"entries":[{"id":"task-42","name":"Review deployment","description":"Waiting for input","number":"611023"}]}
```

The update replaces that directory's entries atomically. IDs must be unique and
phone numbers must be numeric. Names are XML escaped. The handset feed uses
CiscoIPPhoneMenu and CiscoIPPhoneDirectory XML, with 25 entries per page.
An app may omit directories entirely. Built-in directories can be renamed or
disabled, while their entries remain maintained by their integrations.

On the handset, press **Directories**, choose an application directory, select
an entry, and press **Dial**. **Amp sessions** and **Codex sessions** return to
their respective saved conversations;
**Recent Slack huddles** calls a previously connected contact. Apps without a
directory do not add a menu item.

## Speech

Upload audio to `POST /api/v1/speech/transcribe` as multipart form data with
`file`, optionally `provider` and `language`. The result contains `text` and the
provider used. Uploads are limited to 25 MiB and audio is not saved by Switchboard.

`POST /api/v1/speech/synthesize` accepts `{"text":"Hello","provider":"piper"}`
and returns audio. Provider IDs are `whisper`, `elevenlabs-stt`, `piper`, and
`elevenlabs-tts`. Defaults and route overrides are configurable in the dashboard.
For phone recognition, `POST /api/v1/speech/phone/transcribe` accepts
`{"audio":"<base64 PCM>","sample_rate":8000,"route":"codex"}`. The PCM is
signed 16-bit mono audio at 8 kHz; the response is JSON containing `text` and
`provider`. For phone playback, `POST /api/v1/speech/phone/synthesize` accepts
`{"text":"Hello","route":"codex"}` and returns an 8 kHz mono WAV.

Whisper and Piper load lazily in the central process. Phone services delegate
speech to it rather than each loading their own copy of the models. External
provider errors do not silently switch to another cloud service or retry actions.

For continuous conversation, configure **ElevenLabs Speech Engine** under
Integrations, then edit **Talk to Amp** or **Slack operator** under Phone routes.
Set the voice, greeting and operator instructions; **Save and provision engine**
creates or updates a separate Speech Engine for that route. Select **ElevenLabs
Speech Engine** as its phone conversation mode and save. A voice preview is
available in the route editor. Advanced settings expose the SDK's voice,
recognition, turn-taking and conversation options with server-side validation.
The phone's AudioSocket routing and authenticated public upstream must also be
deployed; see [Speech Engine setup](SPEECH-ENGINE.md).

The main phone route retains the ID `codex` for existing speech API clients.
Its display name and integration are now Amp. Custom dial aliases, enabled
states, speech providers and existing profiles survive this migration.

## Slack operators and tools

Create a generic operator with `integration: "slack-huddles"` to give it the
bundled Slack tool instructions. The phone's operator also uses this API.
Actual audio remains in the native huddle bridge; the model is not processing
or recording a huddle conversation.

The installed `switchboard` CLI reads credentials from a private file. On the
phone VM, run it as `codex-phone` (or root) using its full path:

```sh
/opt/phone-platform/current/bin/switchboard session list
/opt/phone-platform/current/bin/switchboard huddle lookup 'a name'
/opt/phone-platform/current/bin/switchboard uuid
/opt/phone-platform/current/bin/switchboard huddle dial U12345678 --request-id '<the generated UUID>'
/opt/phone-platform/current/bin/switchboard huddle status '<the same UUID>'
```

Dialing is an action. Keep its request ID and inspect status after an uncertain
response; never blindly redial. Lookup alone does not call or message anyone.

## Development and deployment

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m phone_platform
PYTHONPATH=. .venv/bin/python -m pytest tests -q
```

Local defaults bind to `127.0.0.1:8088` and keep state in `./state`. Set
`PHONE_PLATFORM_VOICE_SOCKET` to an existing same-user phone control socket to
connect sessions. Local speech additionally requires the phone voice runtime
and models. Unavailable integrations are shown explicitly in the dashboard.

Proxmox deployment is managed in
`/home/raygen/homelab/pulumi-homelab/vms/phone-platform/`. Its README covers source
snapshots, Pulumi previews, activation, phone directory provisioning and rollback.
Existing voice, huddle and session data are retained outside deployment releases.

Protocol references: [Codex app server](https://learn.chatgpt.com/docs/app-server),
[ElevenLabs transcription](https://elevenlabs.io/docs/api-reference/speech-to-text/convert),
[ElevenLabs synthesis](https://elevenlabs.io/docs/api-reference/text-to-speech/convert),
and [Cisco phone XML](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70obj.html).

## Attention inbox and saved tasks

The Attention inbox collects pending questions and sessions in waiting, error,
or interrupted states. Answers use the existing question API. Notifications link
to `/#attention/codex%3A123`, opening that session after sign-in.

Browser notifications are opt-in and need HTTPS or localhost, with the dashboard
open. For the current HTTP home-network address, run the native Linux companion
from `phone-platform`: `.venv/bin/python -m phone_platform.notifier`.
It reads the existing private client registration, polls every five seconds,
remembers delivered items across restarts, and offers an “Open question” action.
Notification text omits task contents. A desktop notification daemon must support
actions for click-through. `--open` opens the inbox manually. The optional user
unit is `deploy/switchboard-notifier.service`; install it in
`~/.config/systemd/user/` and enable it after configuring the desired API address.

Quiet hours use an IANA time zone and follow daylight saving changes. They hold
requests to `/sessions/{id}/callback`, including plain callbacks while the phone
is busy. Queues persist across restarts. Cancellation is available before
submission. A request whose result is uncertain is never automatically retried;
inspect the session before requesting another call. Reuse an `Idempotency-Key`
for the same callback. Existing callers still receive the question result when
immediately submitted; deferred calls return a queue record. Native bridge/MCP questions also respect the schedule after deploying the staged
`voice/callback_policy.py`, control and session-manager changes with
`PHONE_PLATFORM_CALLBACK_POLICY=1` on the bridge. Policy outages defer ringing;
questions remain available for browser answers. Incoming calls are unaffected.
The running bridge is unchanged until that deployment.

Saved tasks contain a prompt, standing instructions, execution host, optional
project folder, and an `89xxxx` speed-dial number. Each start creates a fresh
session; retries of a launch must reuse its `Idempotency-Key`. The dashboard can
create, edit, disable, and run tasks. Enabled tasks appear in the handset's Saved
tasks directory. Phone dialing additionally requires the staged AGI wrapper
`deploy/switchboard-task`, the package/runtime and private client registration
inside the PBX runtime, and `deploy/saved-tasks.conf` included from
`sccp-internal`. These changes are not applied to the running PBX by this build.
The AGI launch uses the channel unique ID to avoid duplicate tasks.

New API routes: `/attention`, `/quiet-hours`, `/callbacks`, `/task-templates`,
and `/task-templates/{number}/run`. See `/api/docs` for schemas.
