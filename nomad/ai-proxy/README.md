# ai-proxy on Nomad

One application allocation runs on the homelab Nomad client, VM230
(`192.168.0.230`). The pinned release is `ai-proxy:proxmox-77deb4b95f4b`.
The image is transferred with `docker save` / `docker load`; keep the exact
revision image available locally before submitting the job.

The public origin remains `https://relay.raygen.dev`. Traffic follows the
existing Cloudflare tunnel through cloudflared (`192.168.0.204`) and Traefik
(`192.168.0.203`) to the allocation's dynamic HTTP port. The Traefik route
accepts only the tunnel host. Packet inspection verified that the application
sees Traefik's original `192.168.0.203` address through Nomad bridge networking;
the environment therefore trusts that exact peer for `CF-Connecting-IP`.

## Configuration

Pulumi configuration uses the `ai-proxy` namespace:

| Key | Purpose |
| --- | --- |
| `enabled` | Register storage and, when an image is set, the application. |
| `sshHostKey` | VM230's SSH host public key, verified through Proxmox. |
| `image` | Unique revision tag or immutable image digest. |
| `environment` | **Secret:** complete production dotenv file. |
| `replicas` | `0` for offline maintenance, `1` for the application. |
| `servingEnabled` | `false` for standby; `true` only for the sole active copy. |

Set `environment` through stdin from a restricted file, never through a
literal command-line value. It must preserve the original `BETTER_AUTH_SECRET`,
`HACKCLUB_CLIENT_ID`, `HACKCLUB_CLIENT_SECRET`, `CODEX_TOKEN_KEY`, and owner
identity. Process variables in the job override the file's standby setting.
The environment revision triggers a restart when configuration changes.

Persistent paths on VM230:

| Host path | Container path | Ownership and mode |
| --- | --- | --- |
| `/opt/nomad/volumes/ai_proxy_data` | `/data` | UID/GID 1000, `0700` |
| `/etc/ai-proxy/ai-proxy.env` | `/run/secrets/ai-proxy.env` | UID/GID 1000, `0400`; read-only mount |

The database is `/data/ai-proxy.sqlite`, mode `0600`. The container runs as
1000:1000 with a read-only root filesystem, all capabilities dropped, and
no-new-privileges. Nomad reserves 200 MHz and 512 MiB. Both the job termination
timeout and client maximum are 45 seconds. Host storage is deliberately retained
when resources are removed.

## Deployment and maintenance

Preview every change and target these resources when other homelab work is in
progress:

```text
urn:pulumi:homelab::pulumi-homelab::command:remote:Command::ai-proxy-storage
urn:pulumi:homelab::pulumi-homelab::command:remote:Command::ai-proxy-environment
urn:pulumi:homelab::pulumi-homelab::nomad:index/job:Job::ai-proxy
```

If the local SSH agent socket is unavailable, run Pulumi with
`env -u SSH_AUTH_SOCK`; the command provider has the explicit private key.

### Recovering the JSON/HCL refresh mismatch

The HCL file remains the source of truth. Pulumi parses it through Nomad's
read-only job parser and registers the result with `json: true`. This matches
the API JSON that Nomad may save as a submission after an external update.
The parser needs the same Nomad connectivity as the job provider.

Older state can contain a JSON `jobspec` with `json: false`. Refresh then fails
on fields such as `Stop` before the updated program runs. A code-only deploy
cannot repair that checkpoint. Do not disable refresh globally or recreate
the running job to work around this.

On an authenticated deployment workstation, with this code checked out and
no concurrent stack operation, the following repairs **only ai-proxy's state**.
It copies the last observed JSON jobspec to its recorded inputs and marks both
inputs and outputs as JSON. The next preview compares that baseline against the
HCL source; it does not adopt the live job as the new source of truth.

**Obtain approval before importing state.** The export contains plaintext
secrets: keep the backup private, do not commit or upload it, and retain it
securely until recovery is verified.

```sh
set -eu
umask 077
repair_dir=$(mktemp -d "${TMPDIR:-/tmp}/ai-proxy-state.XXXXXX")
stack=raygenrrupe-gmail-com/pulumi-homelab/homelab
urn='urn:pulumi:homelab::pulumi-homelab::nomad:index/job:Job::ai-proxy'
pulumi stack export --stack "$stack" --show-secrets --file "$repair_dir/backup.json"

jq --arg urn "$urn" '
  def unsecret:
    if type == "object" and .["4dabf18193072939515e22adb298388d"] == "1b47061264138c4ac30d75fd1eb44270"
    then .value else . end;
  [.deployment.resources[] | select(.urn == $urn)] as $matches |
  if ($matches | length) != 1 then error("Expected exactly one ai-proxy resource")
  elif (($matches[0].outputs.jobspec | unsecret | fromjson | .ID) != "ai-proxy")
  then error("Expected a bare ai-proxy API JSON jobspec; stop and inspect privately")
  else .deployment.resources |= map(
    if .urn == $urn then
      .inputs.jobspec = .outputs.jobspec |
      .inputs.json = true |
      .outputs.json = true
    else . end)
  end
' "$repair_dir/backup.json" > "$repair_dir/repaired.json" &&
pulumi stack import --stack "$stack" --file "$repair_dir/repaired.json"

pulumi preview --stack "$stack" --refresh
```

Stop if validation or import fails. Inspect the preview for unexpected changes
or replacements before authorizing any update. State import itself does not
register, stop, or restart the Nomad job. To undo only the checkpoint repair,
import `backup.json` before any subsequent deployment; that also restores the
original format mismatch.

For database maintenance, apply `replicas=0`, verify that the allocation has
stopped, and take a consistent backup before running the image's offline
`/app/dist-server/db-cli.mjs` commands. Never copy just the SQLite main file
while the application is running. Startup validates schema and migration
state; it does not initialize or migrate the database automatically.

For the D1 migration, the application session owns Worker maintenance and the
final export. Freeze the Worker at 100% on both public and workers.dev
entrypoints, drain for at least six minutes, and import the final export while
the candidate is stopped. Import refuses an existing database or journal.
Verify table contents and secrets before enabling one active allocation.

Standby exposes only `/health`, returning `serving:false`; all other routes
return 503, and upstream access and cleanup are disabled. A healthy standby
does not prove real generation works. Validate the real model catalog, native
CLI model picker and tool execution, and the six buffered/streaming protocol
variants before moving the public hostname.

Cloudflare domain/DNS cutover is coordinated separately from this Pulumi
module: detach the Worker's custom domain, then point its proxied CNAME at the
existing homelab tunnel. Keep the Worker frozen afterward. A rollback must stop
Node and preserve its current database and refreshed credentials before any
Worker activation; do not reactivate the stale D1 copy.

The original routing, secret, and database migration backups are restricted
under `~/.local/state/ai-proxy-migration/20260920T054129Z` on the deployment
workstation. Treat these as credentials and retain them securely.

## Migration completed on 2026-09-20

`relay.raygen.dev` now resolves through the existing homelab tunnel. The old
Worker custom-domain binding was removed; its `workers.dev` endpoint remains
in maintenance (`serving:false`). The Node database is authoritative. Do not
reactivate the old Worker or import its frozen D1 database over Node storage.

Release `77deb4b95f4be497d9e64ea9c52c3b7b35fa91bd` passed the exact real
Docker integration runner both directly and through the public URL: nine
checks each, covering account model discovery, native model picker, Responses,
Chat Completions and Anthropic Messages in JSON and streaming modes, and native
CLI shell write/read with final completion. Existing owner sessions, original
Hack Club callback/PKCE redirect, model catalog parity, invitation lifecycle,
static assets, anonymous admin rejection and public disconnect accounting also
passed. The temporary validation API key was revoked and its file removed.

The final targeted Pulumi preview reported no changes. Deployment reports,
checksums, original routing and restricted database/secret backups are under
the migration directory above. The final D1 export predates Node's live
requests; use a fresh consistent backup of the current Node database for any
future recovery or migration.
