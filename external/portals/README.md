# Runner preview portals

Source: the separate `raygen-portals` repository, currently checked out at
`/home/amp/workspaces/raygen-portals`. The bundled release contains its source
revision in `release.json`. Rebuild there with `npm ci`, `npm run check`,
`npm test`, and `npm run build`; copy `raygen-portals.tgz` here before deploying.
No package registry or public source repository is required.

Routing: Cloudflare's existing free wildcard `*.raygen.dev` tunnel DNS →
cloudflared (204) → Traefik (203) → runner (214:4310). Preview names are
`p-<16 hex digits>.raygen.dev`, not nested `*.p.raygen.dev`, avoiding paid
Advanced Certificate Manager. No Oracle resources or new tunnel are involved.

`portals.raygen.dev` is the owner-only Cloudflare Access login application.
The gateway checks its signed JWT and app audience, then issues a short-lived,
single-use ticket for a host-only preview session. Management uses a private
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

- `cloudflare:index/zeroTrustAccessApplication:ZeroTrustAccessApplication::portals-access`
- `cloudflare:index/dnsRecord:DnsRecord::portals-dns`
- `command:remote:CopyToRemote::portals-bundle`
- `command:remote:Command::portals-install`
- `consul:index/node:Node::portals-node`
- `consul:index/service:Service::portals-service`

Preview before applying. `HOSTED=true` matches the existing stack's provider
configuration, avoiding unrelated Proxmox endpoint changes even on a targeted
update. Do not deploy unrelated runner provisioning or Oracle drift.

Operations: `systemctl status raygen-portals`, `journalctl -u raygen-portals`,
and `raygen-portal list --thread <thread-id>`. State and signing key live in
`/var/lib/raygen-portals`; preserve them across upgrades. Restarting this
service restarts its managed previews, not the Amp runner. Removing the Pulumi
resources removes public routing but does not uninstall the daemon or delete
state: stop/disable `raygen-portals` explicitly if decommissioning it.
