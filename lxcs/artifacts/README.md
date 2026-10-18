# Dedicated artifacts LXC

This auto-discovered module is **disabled by default**. It prepares an owner-only
management/MCP application and public, named artifact sites. The application,
not Cloudflare Access or Authentik, owns authentication and authorization.
No DNS/wildcard changes, synthetic Consul node, automatic resizing, backup job,
or review schedule are created by this module.

## Configuration and binary contract

All keys are in Pulumi namespace `artifacts`:

| Key | Default / requirement |
| --- | --- |
| `enabled` | `false` |
| `vmId` | `216`; verify unused VMID **and** IP before creation; 100..254 |
| `memoryMiB` | `1024`; integer >=256, lower only after resource review |
| `cpuLimit` | `1`; hard quota in cores, >0 and <=1; one virtual CPU |
| `sshHostKey` | enrolled SSH public host key; absent means create LXC only |
| `ownerEmail` | owner identity, required for stage two |
| `bundlePath` | private local directory containing **only** regular file `artifacts` |

Secrets live in Infisical (`prod`, `/artifacts`), not Pulumi config:
`ARTIFACTS_ROOT_PASSWORD` (needed in stage one), `ARTIFACTS_AUTH_SECRET`
(>=32 characters, stable across releases) and `ARTIFACTS_OWNER_PASSWORD`
(bootstrap only, never in the running service environment).

Disk is fixed at 8 GiB, node `optiplex`, unprivileged Debian 13 LXC with nesting
for systemd mount namespaces, starts on boot, protected against replacement.
Networking is `ip(vmId)/16` (default `192.168.0.216/16`), never /24.
SSH uses `ctx.sshKey` / `ctx.sshPrivateKey`. Capability `artifacts-setup` refers
to the final remote provision command; dependency `consul-setup` orders initial
registration after the existing server setup.

The parent prepares an x86-64 Linux compiled Bun native executable, without
secrets embedded. No Bun/npm runtime or global dependencies are installed here.
`artifacts --bootstrap` must migrate existing auth/storage safely and seed the
owner **only if absent**. `artifacts` starts HTTP. The bootstrap subprocess runs
as `artifacts`, with output suppressed and a 300-second timeout. It receives
`ARTIFACTS_OWNER_PASSWORD` in its environment only; the value is neither an
argument nor a file. Provisioning does not reset passwords or replace databases.

Both invocations receive:

```text
ARTIFACTS_ORIGIN=https://artifacts.raygen.dev
ARTIFACTS_HOST_SUFFIX=-artifact.raygen.dev
ARTIFACTS_DATA_DIR=/var/lib/artifacts
ARTIFACTS_OWNER_EMAIL=<ownerEmail>
BETTER_AUTH_SECRET=<authSecret>
HOST=0.0.0.0
PORT=3000
NODE_ENV=production
```

The app must enforce owner-only management/MCP, reject unknown hosts, isolate
public artifacts from management cookies/origin, and never log credentials,
session cookies, authorization headers or secret query parameters. Service
hardening permits Bun JIT (no `MemoryDenyWriteExecute`). The executable must
work with only `/var/lib/artifacts` and private `/tmp` writable. Provisioning
checks HTTP 200 at `/healthz` with `Host: artifacts.raygen.dev` before publishing.

## Two-stage enrollment and scoped deployment

These are operator instructions, **not actions taken by adding this module**.
Set the Infisical secrets above through the Infisical UI, not shell arguments.
Never paste their values or unredacted config exports into a thread.

1. Verify VMID/IP availability on optiplex and preserve unrelated dirty work.
   Set enabled and stage-one config, leaving `sshHostKey` unset. Review the
   scoped preview for only the artifacts component/container. In a normal
   stack the URNs are:

   ```text
   urn:pulumi:<stack>::<project>::homelab:framework:ProxmoxMachine::artifacts
   urn:pulumi:<stack>::<project>::homelab:framework:ProxmoxMachine$proxmoxve:index/containerLegacy:ContainerLegacy::artifacts
   ```

   Verify exact provider token/URN from `pulumi preview --json` rather than
   guessing if the installed provider differs. Use `pulumi preview --target
   '<component-URN>' --target '<container-URN>'` and the **same explicit targets**
   when applying. Avoid `--target-dependents`, `--target '*'`, or a full-stack
   apply: those can include unrelated amp-runner/hashistack changes.
2. Through the trusted **Proxmox console/SSH on optiplex**, run:

   ```sh
   pct exec 216 -- cat /etc/ssh/ssh_host_ed25519_key.pub
   pct exec 216 -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Record the verified public key as `artifacts:sshHostKey`; do not trust
   `ssh-keyscan` alone, and never disable host-key verification. Substitute the
   configured ID if changed. There are no SSH provisioning resources until
   this key exists.
3. Prepare a private, stable bundle directory (0700) containing only the native
   executable. Keep the directory unchanged throughout preview/apply. Set
   stage-two config, preview the exact artifacts resources and inspect all
   creates/updates. Stage-two child URNs use component prefix
   `homelab:framework:ProxmoxMachine$` and resource types/names:

   ```text
   command:remote:Command       artifacts-prepare
   command:remote:CopyToRemote  artifacts-bundle
   command:remote:Command       artifacts-provision
   ```

   Explicitly target all three plus component/container as needed. Existing
   `consul-setup` should already be deployed; do not deploy unrelated Consul
   changes to satisfy a target. Do not apply until scoped preview is clean.

The binary alone is SHA256-hashed. `FileArchive`/`CopyToRemote` copies the
single-file directory over pinned SSH into `/opt/artifacts/incoming/<hash>/`.
Provisioning verifies the checksum and creates an immutable (root-owned,
never rewritten in-place) `/opt/artifacts/releases/<hash>/artifacts`. Existing
release bytes are verified rather than overwritten. `current` is atomically
switched after bootstrap. All secrets are read from Infisical and travel as Pulumi secret stdin
JSON, remote logging is `none`, stdout/stderr are additional secret outputs.
The private input bundle and secret Pulumi state still require normal protection.

## Files, health and routing

| Container path | Purpose |
| --- | --- |
| `/etc/artifacts/app.env` | root-only 0600; no owner password |
| `/var/lib/artifacts/` | app-owned 0700 persistent state; umask 0077 makes files 0600 |
| `/opt/artifacts/releases/<sha256>/artifacts` | root-owned 0755 binary, immutable release |
| `/opt/artifacts/current` | active release symlink |
| `/opt/artifacts/incoming/<sha256>/<bundle-basename>/artifacts` | private staging copy, retained for idempotent reprovisioning |
| `/etc/consul.d/agent.json` | local client, homelab DC, joins 192.168.0.201 |
| `/etc/consul.d/artifacts-service.json` | agent-owned service and HTTP check |
| `/var/lib/consul/` | consul-owned 0700 client state |
| `/etc/systemd/system/artifacts*.service`, `artifacts-usage.timer` | app, Consul client, resource sampler |
| `/usr/local/sbin/artifacts-usage` | executable sample/report command |
| `/var/log/artifacts-usage/YYYY-MM-DD.jsonl` | UTC daily resource samples, root 0600 in 0700 directory |
| `/etc/systemd/journald.conf.d/artifacts.conf` | dedicated-container journal: persistent, 64 MiB / 14 days, rate-limited |

Consul 1.20.1 is fetched from HashiCorp over HTTPS and checked against that
release's HTTPS SHA256SUMS. Its HTTP/DNS client listeners bind loopback; LAN
gossip uses the container IP. It registers **its own** service; no synthetic
node or duplicate external catalog registration exists. Service starts
critical and only a passing local HTTP check makes it routable. The route is
removed before bootstrap on updates and remains closed if bootstrap/health
fails. Traefik catalog reconciliation may take 15 seconds.

Router priority **200** beats shared Authentik outpost priority 100 even for
`/outpost.goauthentik.io/` paths on artifact hosts. Public Host is preserved.
Only the following are allowed, with no auth middleware/Access application:

```text
artifacts.raygen.dev
^[a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?-artifact\.raygen\.dev$
```

After deployment, verify on the guest:

```sh
systemctl status artifacts artifacts-consul artifacts-usage.timer
curl -f -H 'Host: artifacts.raygen.dev' http://127.0.0.1:3000/healthz
curl -f http://127.0.0.1:8500/v1/agent/services
curl -f http://127.0.0.1:8500/v1/agent/checks
ss -lntup  # Consul client API must not listen on LAN
journalctl -u artifacts --since '1 hour ago'
/usr/local/sbin/artifacts-usage report --hours 72
```

Then externally verify management auth, unauthenticated named artifacts,
MCP app-owned auth, unknown-host rejection, and that artifact outpost-shaped
paths never land on Authentik. Wildcard raygen.dev DNS/TLS is pre-existing;
this module does not change it.

## Resource data and 3-day review

The root sampler runs every 60 seconds. It reads the **LXC cgroup-v2 namespace
root** `/sys/fs/cgroup`, not its service child cgroup and never host-global
`/proc/meminfo` or `/proc/stat`. This guest exposes aggregate usage but Proxmox
enforces limits in an ancestor outside its cgroup namespace: `memory.max` and
the CPU quota read `max` inside the guest. **These are not effective limits.**
The report flags `limits.host_verification_required`; guest zero throttling
or limit-hit counters cannot prove the parent has never throttled or hit its
limit. Missing files remain null with an error, never host-total fallbacks.

Before resizing, supplement minute guest logs with authenticated Proxmox API
reads of `nodes/optiplex/lxc/216/config`, `status/current`, and
`rrddata?timeframe=week&cf=MAX`. Proxmox already retains CPU, memory, pressure,
and allocated-limit history outside the guest. Confirm parent cgroup lifetime
OOM/throttling counters through trusted host console access if a reduction is
otherwise justified. Counters are only meaningful when the cgroup has existed
throughout the review window. If that evidence is unavailable, defer resizing;
do not infer zero pressure from invisible ancestor counters.

Samples include `memory.current/peak/max/stat/events`, CPU counters/quota,
CPU/memory/IO PSI, swap usage/limit (`"max"` explicitly means unlimited),
statvfs disk/inodes, container systemd-manager uptime and app status/restarts.
Working set is `max(0, memory.current - memory.stat.inactive_file)`: **not**
anonymous memory alone. Raw `anon` and `file` remain separate. It includes
Consul/system overhead and active cache. `memory.peak` is the kernel's lifetime
high-water mark, separately from sampled peaks. No environment/process args
or request content enters resource JSON.

Daily logs retain the current UTC day plus the preceding 14-day boundary;
each file is capped at 16 MiB (at most 240 MiB). Hitting a cap fails that sample
visibly in the journal rather than filling disk. Samples older than 14 days
are pruned on subsequent sampling. The persistent timer records once after a
missed run; it does not fabricate backfill. Report an exported private copy with:

```sh
python3 lxcs/artifacts/usage.py report --directory /private/copied-usage --hours 72
```

The JSON report includes coverage, missing-minute gaps, window edge gaps,
nearest-rank p95/peak working and total memory, observed CPU-core rates from
counter differences divided by monotonic elapsed time, counter resets,
throttling/OOM deltas, memory event peaks (including a nonzero initial OOM
counter), PSI peaks and total-stall deltas, minimum disk/inode headroom and read/parse
errors. Missing counter intervals are **not** evidence of zero pressure/OOM;
inspect `counter_intervals`, raw event counters and the raw PSI totals too.
An interval spanning a gap is only an average, not a burst peak.

The parent schedules Amp review after **3 days**, with the owner's permission
to lower excessive CPU/RAM. It must set the exact sizing rule. Require at least
**48 hours of representative, substantially complete samples**, no OOM,
pressure or throttling, and retain **at least 2x observed peak working memory**
with additional allowance for cache growth, upload peaks and Consul overhead.
Do not lower solely from p95 or anon/RSS. Missing data, recent reboots, resets,
idle-only workloads or service failures mean defer. **Never shrink disk.**
The sampler itself never changes config or infrastructure. Preview exact
container changes before any later authorized CPU/RAM reduction and confirm
the cgroup limits afterward; no replacement is acceptable.

## Backup and rollback

Before an application upgrade, stop the app during a coherent backup of **all**
`/var/lib/artifacts` (database, WAL/SHM when relevant, uploaded objects), and
back up `/etc/artifacts/app.env` securely. Prefer a consistent Proxmox backup
or app-supported backup procedure; raw copying a live SQLite database is not
a safe backup. Preserve permissions and test restore privately. No backup is
automatically configured by this module.

For rollback, first withdraw `/etc/consul.d/artifacts-service.json` and reload
`artifacts-consul`; wait for the catalog route to disappear. Stop the app.
Select a known-good release, atomically replace `current` using a temporary
symlink and `mv -T`, then start and check health before restoring registration.
Database migrations may be irreversible: restore the matching coherent data
backup if schema compatibility is not guaranteed. Do **not** blindly replace
an existing database. Preserve authSecret. Update the parent bundle/config to
match the rollback before another Pulumi run. Retained releases/staging consume
disk: prune only obsolete, non-current releases/copies after backups and review;
never delete active binaries or data. Reprovisioning a release whose staging
was manually pruned requires replacing `artifacts-bundle` as well.

## Local checks (no infrastructure)

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s lxcs/artifacts -v
node_modules/.bin/tsc --noEmit
```

Tests use fixture cgroups, sample times/counters and temporary files. They
cover missing metrics, anonymous/file distinction, p95 boundaries, resets,
gaps, retention/caps/modes, secret environment rendering, route/health tags,
checksum rejection and release traversal with restrictive umask. Live LXC,
binary compatibility, Proxmox availability, SSH enrollment, public auth and
end-to-end routing require the later authorized deployment.

## Deployed instance and owner access

Deployed September 27, 2026: LXC216 on optiplex, `192.168.0.216/16`, one CPU,
1024 MiB RAM, 8 GiB disk. Pulumi protects against deletion/replacement; this is
distinct from Proxmox's own protection flag. Public health, OAuth PKCE, MCP
creation/publication, Consul health and minute samples were checked live.

- Management: `https://artifacts.raygen.dev`
- Register in Amp: `https://artifacts.raygen.dev/mcp`, using OAuth
- Read the configured login identity with
  `pulumi config get --stack homelab artifacts:ownerEmail`; no mailbox is needed.
- Generate a private one-use, 30-minute `/signup#token=…` link by running
  `/opt/artifacts/current/artifacts --setup-link` as the `artifacts` user with
  the service environment. Load `/etc/artifacts/app.env` using a proper
  EnvironmentFile parser, not by sourcing it as a shell script. Capture stdout
  privately, outside service logs, and share the link only with the owner.
  A new link invalidates old setup links; saving the password revokes browser
  sessions, but not MCP grants. No password or link belongs in Git.
- `ARTIFACTS_OWNER_PASSWORD` remains the initial bootstrap value,
  not necessarily the current password. Bootstrap never resets existing users.
- Public synthetic example: `https://welcome-artifact.raygen.dev`

Source lives in `lordbagel42/agent-tools`, workspace `packages/artifacts`.
The release is built from the local checkout, not a pushed Git revision.
The bootstrap password and signing secret live in Infisical.

Owner email maintenance on September 27 retained the existing user ID, account
credentials, artifact and both revisions. A coherent SQLite backup and previous
environment are retained privately in
`/var/backups/artifacts/owner-setup-20260927T122235Z/`. The database, service
environment and Pulumi owner identity must agree; changing config alone does not
migrate the user. Existing browser sessions and password-setup tokens were
revoked during maintenance. Resource sampling and the three-day review were not
changed by the owner-access update.
