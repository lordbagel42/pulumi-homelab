# June

June runs in a dedicated, persistent, unprivileged Debian LXC on **optiplex**:

- VMID **215**, `192.168.0.215/16`
- **2** CPU cores, **2048 MiB** RAM, **12 GiB** `local-lvm` disk
- start on boot and Pulumi deletion protection
- Node.js **24.21.0** and pnpm **10.33.0**

The service listens on `http://192.168.0.215:3080`. Slack DMs and native reactions
are live. Consul/Traefik and the existing Cloudflare tunnel route only POST to
`https://june-slack.bagelindustries.com/webhooks/slack`; June verifies each request's
Slack signature. GET, `/health`, `/operator/*`, and the Rivet engine/peer/metrics
ports remain private. Do not use `june.bagelindustries.com`: that hostname already
belongs to a different application.

Without the optional `june:slack` configuration, setup mode starts no messaging
adapters or public ingress. WhatsApp and native coding remain disabled.

## Source artifact

The reviewed public source snapshot is stored at `lxcs/june/source.tar.gz`, so
deployment does not depend on an absolute path on a particular runner. June's
source checkout is still unpushed, not a release that can be cloned on the
container. Build replacement archives from the checkout that owns the source.
The archive root may contain only `.node-version`, `.npmrc`,
`package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, `tsconfig.json`, and
`src/`; the first two files are optional. Symlinks, credentials, generated data,
and `node_modules` are rejected.

From this repository root, rebuild the snapshot from the current local checkout:

```sh
archive=lxcs/june/source.tar.gz
tar --create --gzip --file "$archive" \
  --directory /home/amp/workspaces/agent \
  --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  --exclude='*.test.ts' \
  .node-version .npmrc package.json pnpm-lock.yaml pnpm-workspace.yaml \
  tsconfig.json src
python3 lxcs/june/provision.py --validate-source-archive "$archive"
sha256sum "$archive"
```

The provisioner derives the release identity from the actual archive bytes. An
optional `june:sourceArchiveSha256` config value can pin the reviewed digest and
makes a mismatch fail during preview.

## Bootstrap and SSH enrollment

Run commands from the repository root. Set secrets interactively so they do not
enter shell history:

```sh
pulumi config set june:enabled true
pulumi config set --secret june:rootPassword
```

Review and create only the protected container:

```sh
bash lxcs/june/pulumi-service.sh machine preview
bash lxcs/june/pulumi-service.sh machine up
```

Before any credential or source crosses SSH, open the **optiplex Proxmox host
console** and read the key through the container console:

```sh
pct exec 215 -- cat /etc/ssh/ssh_host_ed25519_key.pub
pct exec 215 -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Compare that trusted key with the Ed25519 key seen at `192.168.0.215`, then save
the complete trusted `ssh-ed25519 ...` line. Do not accept an unverified scan and
do not disable host-key checking.

```sh
pulumi config set june:sshHostKey
pulumi config set --secret june:operatorToken
pulumi config set june:sourceArchive lxcs/june/source.tar.gz
pulumi config set june:sourceArchiveSha256 "$(sha256sum lxcs/june/source.tar.gz | cut -d' ' -f1)"
bash lxcs/june/pulumi-service.sh service preview
bash lxcs/june/pulumi-service.sh service up
```

The operator token must be a random URL-safe value of 32-256 characters. The
helper accepts only `preview` and `up`, and selects only June resource URNs.
`all` selects both machine and service resources after enrollment. Always review
the preview; never run an untargeted update merely to deploy June.

Provisioning uses the stack SSH private key, the pinned host key, disabled remote
command logging, secret stdin, and secret stdout/stderr. `/etc/june/credentials`
is root-owned mode 0600 and contains `JUNE_OPERATOR_TOKEN`, plus
`SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` when Slack is configured. No CLI
management, WhatsApp, or model API credential is installed.

The generated config owns the LAN listener and identifies the owner as `raygen`,
with the configured Slack identity or no identities in setup mode. It selects
Codex model `gpt-6-astra`, uses
`/var/lib/june/.codex` and the release's pinned Codex executable, and keeps native
coding disabled.

## Runtime and updates

The pinned Node.js archive is downloaded directly from nodejs.org and verified
against the published SHA-256 before extraction; no installer is piped to a
shell. Corepack runs exactly pnpm 10.33.0, and `pnpm install --frozen-lockfile
--prod=false` installs the lockfile's runtime and development dependencies,
including `tsx`.

Each source digest becomes an immutable root-owned directory under
`/opt/june/releases/`; `/opt/june/current` is an atomic symlink to the active
release. The service runs as `june` from that link. Private durable data remains
outside releases:

- home: `/var/lib/june`
- Codex login: `/var/lib/june/.codex`
- Rivet data: `/var/lib/june/rivet`
- config: `/etc/june/config.json`
- credential environment: `/etc/june/credentials`

The self-hosted Rivet engine binds loopback port **6420**. Rivet normally detaches
its engine process; `june.service` uses `KillMode=control-group`, so stop/restart
terminates every descendant before systemd starts another instance. The service
has a read-only system filesystem and can write only its private state plus its
private temporary directories.

For an update, create and validate a new archive, update `sourceArchive` and its
optional digest, then run the scoped service preview and update. Provisioning
installs the new release before switching `current`. It requires `/health` to
return `{"name":"June","ready":true}`. If that fails, it restores the former
link, config, credential file, and service, then restarts the former release.
Failed and previous release directories are retained; persistent state and Codex
login are never removed.

Inspect operation on the LAN:

```sh
curl --fail --silent http://192.168.0.215:3080/health
ssh root@192.168.0.215 systemctl status june.service
ssh root@192.168.0.215 journalctl -u june.service -f
```

Readiness means the June HTTP/Rivet runtime is ready. It intentionally does not
claim that Codex is signed in or that model inference works.

## Slack administration

Slack CLI **4.8.0** is installed at `/home/amp/.local/bin/slack` on
**homelab-amp** (`192.168.0.214`), not in June's container. Its official GitHub
release archive was verified against the release's published SHA-256 digest.
Use June's separate mode-0700 CLI profile, outside both repositories:

```sh
umask 077
slack login --config-dir /home/amp/.config/june/slack-cli --skip-update
slack auth list --config-dir /home/amp/.config/june/slack-cli --skip-update
```

Run the printed `/slackauthticket` command inside the intended workspace, approve
Slack's permission prompt, and enter its challenge code in the CLI terminal.
Do not put the challenge or saved credentials in Git or chat. June is installed
as app `A0C4749KM3R` in workspace `T0266FRGM`; its owner identity is `U08R4KDL6UF`.
Keep CLI administration credentials on the runner; only June's bot credentials
belong in the service.
June's runtime stays self-hosted rather than moving to Slack-hosted compute.

`june:slack` holds the workspace `teamId`, `botUserId`, and human `ownerUserId`.
The bot token and signing secret use encrypted stack keys `june:slackBotToken`
and `june:slackSigningSecret`. Slack configuration disables setup mode and creates
the narrowly scoped webhook ingress. Preview the `all` phase when adding it.

Search is disabled. The app's updated manifest requests bot `search:read.public`
and user `search:read.public`, `search:read.private`, and `search:read.im`; admin
approval and separate user consent are still required. These scopes do not add
private-search support to the current service. Do not enroll CLI management
tokens as user search credentials.

## Codex browser sign-in

Use the official local callback flow, never device-code login. On the computer
where the browser will open, keep this tunnel running:

```sh
ssh -N -L 127.0.0.1:1455:127.0.0.1:1455 root@192.168.0.215
```

In a second pinned SSH session, start the installed transient login unit:

```sh
ssh -t root@192.168.0.215 /usr/local/sbin/june-start-login
```

Open the printed URL in the browser on the tunnel host. The transient systemd
unit runs as `june` with `HOME=/var/lib/june` and
`CODEX_HOME=/var/lib/june/.codex`, and survives an SSH disconnect for up to 20
minutes. Its root-only mode-0600 output is `/run/june-browser-login/output.log`; do not copy
that output or any credential into Git or general logs. Check status without
changing homes:

```sh
ssh root@192.168.0.215 \
  'runuser -u june -- env HOME=/var/lib/june CODEX_HOME=/var/lib/june/.codex PATH=/opt/node-v24.21.0/bin:/usr/local/bin:/usr/bin:/bin /opt/june/current/node_modules/.bin/codex login status'
```

Back up `/var/lib/june` before host maintenance. The current setup has no retention
policy or automated backup; adding those is a separate reviewed deployment. Do
not import sensitive historical accounts before those controls are implemented.
