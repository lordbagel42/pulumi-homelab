# Control Home Assistant from extension 555

The production service runs on Proxmox VM 233 and uses
[Switchboard](../phone-platform/README.md) for speech and the Home Assistant
conversation API. Its speech provider can be changed in the dashboard. Home
Assistant is connected without registering a phone directory.

Dial **555**, wait for the greeting, then say a command such as “turn on the
kitchen lights,” “set the bedroom lights to fifty percent,” or “is the desk
lamp on?” Pause for about a second after speaking. Wait for the reply before
speaking again, and hang up when finished. No wake word is needed. Calls are
limited to 30 minutes and one simultaneous Home Assistant caller.

The greeting is **“Home assistant, what's up?”** This installation uses
the **ChatGPT** conversation agent through Proxmox LXC **213**, which has its
own browser-authorized ChatGPT login through Codex.
See [ChatGPT connection and service details](CHATGPT.md).

The speech bridge runs on **Proxmox VM 233, 192.168.0.233**, alongside
extension 611 and Asterisk. Whisper and Piper run there using
`/opt/phone-voice/venv` and `/var/lib/codex-phone/models`.
Recognized commands go directly to Home Assistant's
Assist conversation API; its response is read back over the phone. Each call
has a separate conversation. By default, Home Assistant selects its built-in
agent. To select another agent, set `agent_id` to its conversation entity ID.
English is used for recognition and commands.

## Connect your Home Assistant

For a local development or recovery configuration, run from the project directory:

```bash
voice/.venv/bin/python voice/configure_home_assistant.py
```

Enter your Home Assistant base URL, such as `http://homeassistant.local:8123`,
and a **Long-Lived Access Token** from your Home Assistant profile's Security
tab. Token entry is hidden. The helper checks the API without changing devices
and saves `voice/home-assistant.json` with owner-only permissions (mode 600).
The file is ignored by Git. Production configuration is the encrypted
`phone-voice:homeAssistantConfig` value in the homelab Pulumi stack, installed
at `/var/lib/codex-phone/home-assistant.json` on VM 233. Update that stack
configuration and deploy the module to change the production connection.
Configuration is read on each call.

In Home Assistant, open **Settings → Voice assistants → Expose** and enable
**Assist** for the devices you want to control. Use their configured names,
aliases, or room names in commands. Commands supported by your selected
conversation agent are available from the phone.

For manual setup, use `home-assistant.example.json` as a template. A
`token_file` property can replace `token`; relative paths are resolved from
the config file's directory. Restrict the token file to mode 600 too.
`HOME_ASSISTANT_CONFIG`, `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN`,
`HOME_ASSISTANT_TOKEN_FILE`, and `HOME_ASSISTANT_AGENT_ID` can override the
saved configuration. Production environment settings belong in the
Pulumi-managed system service. An explicit `HOME_ASSISTANT_TOKEN` takes
precedence over a token file. The optional `timeout` is in seconds (default 20).

Check an existing connection without controlling devices:

```bash
voice/.venv/bin/python voice/configure_home_assistant.py --check
```

## Service

The system service on VM 233 listens on `127.0.0.1:9093`; Asterisk routes 555
to it using AudioSocket. Pulumi installs and enables the service. The original
workstation user service is disabled and retained for rollback.
No cloud speech service or OpenAI API key is needed.

Run these commands on VM 233:

```bash
systemctl status home-assistant-phone.service
journalctl -u home-assistant-phone.service -f
systemctl restart home-assistant-phone.service
docker exec sccp-pbx asterisk -rx 'dialplan show 555@sccp-internal'
```

If credentials are missing, 555 explains how to finish setup and ends the
call. Connection and authentication errors are spoken instead of claiming
success. Failed or timed-out device commands are never retried automatically;
an action may already have reached a device before a timeout.

Call state is stored in `/var/lib/codex-phone/state/home-assistant/status.json` with
owner-only permissions. This bridge does not save command transcripts,
spoken responses, or recordings. Home Assistant may retain its own records.

## Verification

```bash
voice/.venv/bin/python -m unittest discover -s voice/tests -v
voice/.venv/bin/python voice/smoke_test_home_assistant.py
```

The smoke test uses real local speech models and AudioSocket framing with a
fake Home Assistant API on temporary loopback ports. It does not ring a phone
or control real devices. A live API check is separate, as shown above.

References: [Home Assistant Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/),
[REST API authentication](https://developers.home-assistant.io/docs/api/rest/),
[exposing devices to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/),
and [Asterisk AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).
