# Home Assistant ChatGPT bridge

Dedicated unprivileged Debian LXC **213** on **optiplex**, address
`192.168.0.213`, with 1 CPU, 1 GiB RAM and an 8 GiB disk. It starts on boot.
Pulumi protects the container against accidental deletion because its ChatGPT
login persists in `/var/lib/chatgpt-bridge/.codex`.

The service translates Home Assistant's Ollama conversation requests into
isolated Codex CLI generations. Home Assistant executes proposed device tools.
It uses a ChatGPT subscription login; no OpenAI API key is required. Codex is
pinned to `0.154.0`, downloaded from OpenAI's npm package and verified with its
SHA-512 integrity value. The model is `gpt-6-astra`.

## Deployment

This directory is discovered automatically by the homelab framework. Its stack
configuration is under `chatgpt-bridge`:

- `enabled`: opt in to creating the service.
- `rootPassword`: encrypted container password; normal administration uses SSH.
- `token`: encrypted bearer token shared with Home Assistant's Ollama integration.
- `sshHostKey`: the container's SSH host public key, verified through `pct exec`
  on the Proxmox node before provisioning.

For initial enrollment, deploy the machine first without `sshHostKey`, read
`/etc/ssh/ssh_host_ed25519_key.pub` through `pct exec 213 -- cat ...` on optiplex,
save it with `pulumi config set chatgpt-bridge:sshHostKey`, then preview and deploy
the provisioning resource. The deployment never imports a desktop ChatGPT login.

The Python source is a deployment copy of
`Projects/cisco-phone-shenanigans/voice/chatgpt_gateway.py`. Copy changes here,
run the source project's gateway tests, and review a Pulumi preview before
updating. Changes to the service, source, installer, or encrypted token trigger
provisioning. Provisioning preserves the service user's `.codex` directory.
If your shell points `SSH_AUTH_SOCK` at an unavailable desktop agent, run Pulumi
with `env -u SSH_AUTH_SOCK`; provisioning uses the stack's private SSH key.

## Sign in and operate

Forward the browser callback from the computer where you will sign in:

```sh
ssh -N -L 127.0.0.1:1455:127.0.0.1:1455 root@192.168.0.213
```

Keep that tunnel running. In a second SSH session, run the normal browser login
on the container as its dedicated service account:

```sh
runuser -u chatgpt-bridge -- /usr/local/bin/codex login
runuser -u chatgpt-bridge -- /usr/local/bin/codex login status
systemctl status home-assistant-chatgpt.service
journalctl -u home-assistant-chatgpt.service -f
```

Open the link printed by the login command in the browser on the computer running
the tunnel. The normal browser callback is forwarded securely to the container.
Device code authentication is not enabled on this account; do not use it.
Credentials stay in the service user's private home directory and refresh during use.
Alternatively, pipe `start-login.sh` to a root SSH session on the container. It
starts a temporary systemd login unit, prints the sign-in instructions and keeps
waiting even if SSH disconnects. Its private log is
`/var/lib/chatgpt-bridge/browser-login.log`.

The endpoint is `http://192.168.0.213:11435`, model `chatgpt:latest`. It requires
the shared bearer token and accepts only Home Assistant (`192.168.0.200`), the
phone host (`192.168.0.105`) and local requests. It has no public reverse-proxy
route. The systemd service runs without root privileges, with a read-only system
filesystem and a writable private state directory.

The deployment and browser sign-in were completed on September 20, 2026.
Home Assistant's active `conversation.chatgpt` entity and preferred ChatGPT Assist
profile use this endpoint. A live phone audio test exercised speech recognition,
actual Home Assistant device control through this bridge, and the spoken reply.
The former desktop bridge service and integration are disabled; its integration
is retained as **ChatGPT (desktop backup)**. Asterisk and the 555 speech service
still run on the phone computer, using local Whisper and Piper.

Authentication reference: https://developers.openai.com/codex/auth/
