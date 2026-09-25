# Runner preview portals

Source: [raygen-portals](https://github.com/lordbagel42/raygen-portals), checked out at
`/home/amp/workspaces/raygen-portals`. The bundled release contains its source
revision in `release.json`. Rebuild there with `npm ci`, `npm run check`,
`npm test`, and `npm run build`; copy `raygen-portals.tgz` here before deploying.
No package registry or public source repository is required.

Routing: Cloudflare's existing free wildcard `*.raygen.dev` tunnel DNS →
cloudflared (204) → Traefik (203) → runner (214:4310). Preview names are
`p-<16 hex digits>.raygen.dev`, not nested `*.p.raygen.dev`, avoiding paid
Advanced Certificate Manager. No Oracle resources or new tunnel are involved.

`portals.raygen.dev` serves the built-in PIN login screen, without Cloudflare
Access. A shared PIN is the default; agents can specify a custom PIN per portal
that replaces the shared PIN. Only salted scrypt hashes are persisted. Set the
shared PIN with `raygen-portal set-pin` on the runner (hidden terminal input).
Fresh installations discard a random bootstrap PIN and require that setup step
for shared-PIN access; agent login URLs work immediately.

This deployment's owner requested a recoverable random shared PIN. Its separate
recovery copy is `/etc/raygen-portals/shared-pin-recovery`, owned by root with
mode `0600`, outside Git and Pulumi state. Retrieve it with
`sudo -n cat /etc/raygen-portals/shared-pin-recovery`. Deployments preserve it.
If you rotate the PIN with the CLI, update the recovery copy too; the daemon
does not read or maintain that plaintext backup.

The gateway issues a short-lived, single-use ticket for a host-only preview
session after PIN verification. Management uses a private
Unix socket, not public HTTP. Agent browser login tickets are sensitive.
The signing key is generated locally, never exported into child environments
or Pulumi state. All runner code running as `amp` remains trusted.

The systemd unit supervises previews in a separate cgroup (2 GiB maximum,
one CPU, 512 tasks). It installs `/usr/local/bin/raygen-portal` and the local
Amp plugin `~/.config/amp/plugins/raygen-portals.js`. Reload that single plugin
in an existing thread; new sessions discover it normally. Amp does not support
its native Portals tab on runners.

Use an explicit target for each resource on first deployment; Pulumi wildcard
targets only matched existing resources during testing. Target these URNs under
`urn:pulumi:homelab::pulumi-homelab::`:

- `cloudflare:index/dnsRecord:DnsRecord::portals-dns`
- `command:remote:CopyToRemote::portals-bundle`
- `command:remote:Command::portals-install`
- `consul:index/node:Node::portals-node`
- `consul:index/service:Service::portals-service`

Preview before applying. `HOSTED=true` matches the existing stack's provider
configuration, avoiding unrelated Proxmox endpoint changes even on a targeted
update. Do not deploy unrelated runner provisioning or Oracle drift.

When migrating from Access, first apply only `portals-bundle` and
`portals-install`, leaving the old gate in place. Verify built-in PIN login
against the origin, then preview/apply the portal resources including the old
`cloudflare:index/zeroTrustAccessApplication:ZeroTrustAccessApplication::portals-access`
URN for deletion. Never remove Access while the old daemon is still installed.
The new daemon ignores legacy Access headers and rejects legacy sessions.

Operations: `systemctl status raygen-portals`, `journalctl -u raygen-portals`,
and `raygen-portal list --thread <thread-id>`. State and signing key live in
`/var/lib/raygen-portals`; preserve them across upgrades. Restarting this
service restarts its managed previews, not the Amp runner. Removing the Pulumi
resources removes public routing but does not uninstall the daemon or delete
state: stop/disable `raygen-portals` explicitly if decommissioning it.
