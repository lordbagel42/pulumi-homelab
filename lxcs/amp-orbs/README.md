# Amp orbs on E2B

Outbound-only controller in **unprivileged LXC 218 on Optiplex**, at
`192.168.0.218/16`: one CPU (one-core quota), 768 MiB RAM, 256 MiB swap,
4 GiB disk. Optiplex is `192.168.0.10`; homelab-amp runs on Tower, not Optiplex.
No Cloudflare, DNS, reverse proxy, inbound provider port, Docker daemon, or
personal Amp login is needed. E2B runs the actual orbs (4 CPUs / 8 GiB each).

`dispatch.mjs` is the official [Amp E2B dispatcher](https://ampcode.com/docs/orbs/self-hosted).
It discovers running and paused sandboxes by `orbId` metadata, resumes the same
sandbox, and pauses rather than deletes on the one-hour timeout. E2B preserves
disk and memory. As in the official example, project snapshots (`ORB_SNAPSHOT_ID`)
are not implemented: new threads run their project setup instead of cloning a
prepared project snapshot. The E2B template is fixed; `ORB_IMAGE` is not dynamically
built per request. Rebuild the template deliberately to update the orb image.

## Credentials

Infisical project `923d970a-8e38-48f5-8c38-e7197c1ce36c`, `prod`, `/amp-orbs`:

* `E2B_API_KEY`: required, E2B account credential.
* `AMP_ORB_PROVIDER_TOKEN`: create a provider named `e2b` in the workspace's
  **Settings → Orbs**; enter its one-time `aop_live_…` token directly into Infisical.
  Never paste it in chat, command arguments, or Pulumi config.
* `lxc-root-amp-orbs`: generated root password, managed by `lxcPassword`.

The container's creation-time password is ignored for replacement planning;
the provisioner applies subsequent rotations with `chpasswd`. SSH accepts keys
only, including for root.

Only these service credentials are delivered to the guest. Infisical bootstrap
credentials and the owner's Amp login never enter it. Root-owned 0600 environment
files live in `/etc/amp-orbs` (0700); systemd reads them before dropping to the
locked `amp-orbs` service user. Pulumi passes secrets on pinned SSH stdin with
logging disabled and secret stdout/stderr. Treat Pulumi state as sensitive.

Without the provider token the service is enabled but intentionally skipped by
`ConditionPathExists`; it must not retry authentication with a placeholder.
Select `e2b` when starting a thread after activation. This setup does not change
the workspace's default provider.

## Deploy through pulumi-homelab

This module is auto-discovered, disabled unless `amp-orbs:enabled` is true.
Before first deployment, verify that VMID/IP 218 are free across the cluster and
that Optiplex has resource headroom. Never apply the whole shared stack for this
service. Use the same explicit targets for preview and update; no
`--target-dependents` or wildcard targets.

Stage one targets (stack `homelab`, project `pulumi-homelab`):

```sh
pulumi config set amp-orbs:enabled true --stack homelab
root='urn:pulumi:homelab::pulumi-homelab'
component="$root::homelab:framework:ProxmoxMachine::amp-orbs"
container="$root::homelab:framework:ProxmoxMachine\$proxmoxve:index/containerLegacy:ContainerLegacy::amp-orbs"
password="$root::command:local:Command::lxc-password-amp-orbs"
pulumi preview --stack homelab --target "$password" --target "$component" --target "$container"
# Review: only the three new service resources, then repeat with `pulumi up`.
```

Obtain `/etc/ssh/ssh_host_ed25519_key.pub` via `pct exec 218` over the trusted,
host-key-verified Optiplex SSH connection. Record that public key in
`amp-orbs:sshHostKey`. Do not trust an unverified network scan. Stage two adds:

```sh
key="$root::command:local:Command::read-secret-E2B_API_KEY"
token="$root::command:local:Command::optional-secret-AMP_ORB_PROVIDER_TOKEN"
provision="$root::homelab:framework:ProxmoxMachine\$command:remote:Command::amp-orbs-provision"
pulumi preview --stack homelab --target "$key" --target "$token" --target "$provision"
# Review and repeat with `pulumi up` and the same targets.
```

After entering or rotating an existing Infisical credential, refresh its cached
local-command resource explicitly. For the provider token:

```sh
pulumi preview --stack homelab --replace "$token" --target "$token" --target "$provision"
# Review and repeat with `pulumi up` and the same targets/replace.
```

Provisioning installs checksum-verified Node 22.23.4 (the SDK needs >=22.19),
the current checksum-verified Amp CLI, pnpm 10.33.0, and the frozen E2B 2.58.0
dependency graph. Dependencies and binaries are root-owned. Amp automatic updates
are disabled; redeploy to update the controller. Only `/var/lib/amp-orbs` is
service-writable. systemd restarts on exit and at boot, with a 640 MiB service
memory limit and no Linux capabilities. Journald retention is capped at 32 MiB /
14 days.

## E2B template

Build once with the official CLI and the E2B credential in the environment:

```sh
e2b template create amp-orbs --dockerfile lxcs/amp-orbs/e2b.Dockerfile \
    --cpu-count 4 --memory-mb 8192 --ready-cmd true
```

The template is built in E2B, not on Optiplex. `amp-orbs:template` can select its
alias or template ID; default `amp-orbs`. E2B bills running sandboxes separately
from Amp. Paused sandboxes remain in the E2B account until Amp sends delete.

## Operations

On Optiplex, use `pct exec 218 -- …`:

```sh
systemctl status amp-orbs
systemctl is-enabled amp-orbs
journalctl -u amp-orbs --since '1 hour ago'
systemctl show amp-orbs -p Restart -p NRestarts -p MemoryCurrent
ss -lntup # Only SSH should listen on the LAN.
```

Do not print environment files, dump process environments, or enable verbose
credential logging. A valid provider token is needed to test Amp's job stream
and authenticated bootstrap; E2B create/pause/resume/delete can be verified
independently without creating an Amp thread. Delete only test-owned sandboxes.

Verified on 2026-10-21 from the controller as `amp-orbs`: official dispatcher
spawn/repeated spawn, pause/repeated pause, resume with the same sandbox ID and
preserved file and running process, delete/repeated delete, and pause of a
missing sandbox. The test sandbox was removed. The E2B template build succeeded
with ID `xkmkbuv8tprituh4f9yz`.

Activated on 2026-10-22 through a scoped token-resource refresh and provision.
Authenticated bootstrap was verified against the real user thread
[`Docker orb provider`](https://ampcode.com/threads/T-01a16197-8587-7307-93dd-b75f5b2e8ec9):
the controller completed its spawn, pause and resume jobs, E2B metadata matched
that thread to sandbox `iq6l2hk1gse5x0k7z702m`, and the thread successfully ran
executor tools. No additional test thread was created, and this user sandbox
was left running. A controlled provider SIGTERM verified automatic service
recovery and reconnection without stopping the E2B orb. Runtime credential files
remained root-only; neither current credential appeared in the inspected
service journal or Amp CLI logs.

## Source/state reconciliation

The original deployment and activation were performed in
[this Amp thread](https://ampcode.com/threads/T-01a1600d-1512-75f8-913f-4ce43d5fece4).
It left this module untracked and its stack settings uncommitted in the shared
`/home/amp/workspaces/pulumi-homelab` checkout. Consequently published main did
not own the resources already present in Pulumi state, and previews from main
proposed protected deletion. Protection prevented deletion; this is not proof
that the running provider was broken.

On November 2, the nine service files and enable/verified-host-key settings
were copied into the isolated Sure reconciliation branch with user approval.
The shared checkout and all its unrelated work were left untouched. Existing
Infisical logging protections are already present in main. The recovered source
initially produced an unchanged scoped preview, and live LXC 218 was running
with its service active and enabled. Formatting and an explicit `check=False`
on the existing Node-version probe are the only changes to the recovered code.
These change the provisioner's payload, so a later preview may propose an
in-place provisioning update; do not apply it just to publish the source.

A combined read-only whole-stack preview no longer proposes protected deletions,
but still proposes unrelated updates. No whole-stack update or merge is safe
until those differences are reviewed separately. Keep Sure and this owning
module together in the landing candidate; do not land an incomplete source tree
and allow merge-triggered deployment to run between reconciliation commits.
