# Amp phone sessions

Switchboard runs new Amp sessions on a configured **Amp runner**, while preserving
the native Amp thread ID and link. The bridge uses the documented CLI JSON stream;
the runner executes the work. Hanging up leaves the task running. Existing Codex
histories remain attached to their original engine.

## Phone bridge configuration

The phone service needs a current Amp CLI and its own signed-in Amp identity that
can access the selected runner. Authenticate as the service account using `amp
login`, or provide that account's long-lived access token through the service's
protected `AMP_API_KEY` environment. Do not copy another machine's login file.

| Setting | Default | Purpose |
| --- | --- | --- |
| `AMP_BIN` | `amp` on PATH | CLI executable on the bridge |
| `AMP_RUNNER_ID` | `homelab-amp` | Stable runner name |
| `AMP_RUNNER_DIR` | `/home/amp/workspaces` | Absolute directory served by that runner |
| `AMP_MODE` | `medium` | Mode for new threads |
| `AMP_THREAD_VISIBILITY` | `private` | Visibility for new threads |

`AmpClient.configured` checks local CLI and authentication-file/token presence.
It does **not** prove the login is valid or the remote runner is online. An absent
or unauthenticated runner is reported as an error, never as completed work.

The first request reserves an `amp-pending:` handle. The CLI's first native
`T-…` event atomically replaces it in the session store. If the connection fails
before that binding, the bridge refuses to submit the initial task again, since
Amp may already have accepted it. Inspect Amp before making another session.

## Runner phone plugin

For the existing homelab runner at `192.168.0.214`, run from this checkout:

```sh
python3 voice/install_amp_plugin.py
```

This installer uses the existing `~/.ssh/proxmox-pulumi` key and pinned SSH host
keys. It captures the protected Switchboard token from the phone VM and passes it
directly to the runner over SSH stdin. No token is printed, placed in arguments,
or written to a local file. Directories are `0700`; plugin, config, and token are
`0600`, owned by `amp`. Existing identical files are preserved; conflicting files
abort staging without replacing them. It never starts or restarts the runner and
does not edit the concurrently provisioned runner service or playbook. Its final
status distinguishes staged files, service state, and login-file presence.

For another runner, follow the same private installation procedure manually:

Copy `amp_switchboard_plugin.ts` to the **runner account's**
`~/.config/amp/plugins/switchboard.ts`, then restart or reload Amp plugins. Install
it on each runner that should accept phone steering, cancellation, and callback
questions. It does not change Amp permissions or expose a new listening port.

Create `~/.config/amp/switchboard.json` for that account with:

```json
{
  "url": "http://192.168.0.233:8088",
  "token_file": "/home/amp/.config/amp/switchboard.token"
}
```

The URL is the existing Switchboard base URL, without `/api/v1`. Store a
Switchboard bearer token in `token_file` with mode `0600`, owned by the runner
account. `PHONE_PLATFORM_URL` and `PHONE_PLATFORM_TOKEN_FILE` can supply these
settings instead. `AMP_SWITCHBOARD_CONFIG` selects a different JSON file.
Tokens are read from the file, never put in CLI arguments or plugin logs.

The plugin registers `phone_ask_user` and `phone_get_answer`. These attach to the
current native thread's existing phone session, rather than allocating another
callback extension. Pending/missed calls remain unanswered. The normal Amp event
stream supplies spoken progress and final replies.

The plugin polls authenticated `/api/v1/phone/rpc` methods `amp_poll` and
`amp_ack` for the native threads it has opened. Steering uses the documented
`appendUserMessage(..., {steer: true})`; stopping uses `cancel()` and checks for an
idle/error state before acknowledging success. The bridge's `AmpControl` queue
persists claims, expires unconfirmed requests, and never replays steering after a
lost connection. A lost acknowledgment can be retried without repeating the action.

Without the runner plugin, ordinary conversation still works; updates wait for
the current turn, and phone cancellation reports that stopping is unconfirmed.
Closing the local CLI observer does not stop a remote task. Use the native Amp
thread to inspect or stop work when control delivery is unconfirmed.

## Verification

These checks use a fake CLI and mocked plugin HTTP transport; no model calls,
phone calls, or speech-provider credits are used:

```sh
PYTHONPATH=voice python3 -m unittest discover -s voice/tests -p test_amp_client.py -v
bun test ./voice/tests/test_amp_plugin.ts
```

After both accounts are signed in and the runner is online, verify one short
conversation, its native thread link, continuing the same thread, a clarification
callback, and explicit cancellation before relying on unattended phone work.

Protocol references: [execute mode](https://ampcode.com/docs/cli/execute-mode),
[JSON streaming](https://ampcode.com/docs/cli/streaming-json),
[remote execution](https://ampcode.com/docs/cli/spawning-orbs), and
[the Amp plugin API](https://ampcode.com/docs/plugin-api).
