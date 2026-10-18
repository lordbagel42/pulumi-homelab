# Automatic infrastructure deployment

Pushes to `main` run `.github/workflows/deploy.yml`. Lint must pass before the
hosted runner joins Netbird and runs Pulumi with refresh enabled. Preview uses
the same full-stack scope and artifact preparation as deployment.

## Backup server

Inspiron and PBS VM 100 were verified online on October 18, 2026. A PBS-scoped
refresh/preview showed no VM or disk changes, so the temporary `pbs-*` and
`proxmox-backup-server` exclusions were removed from both workflows. PBS is
managed and refreshed normally again. A running VM or successful infrastructure
deployment does not prove that scheduled backups have completed successfully.

## Application ownership and immutable inputs

- AI-proxy is retired. Removing its Nomad job stops its allocations and route;
  its command resources have no delete actions. The SQLite data at
  `/opt/nomad/volumes/ai_proxy_data`, credentials at `/etc/ai-proxy`, host-volume
  configuration, and Infisical `/ai-proxy` secrets are retained for recovery.
  No old Worker or database copy should be reactivated automatically.
- June's container and ingress remain managed here (LXC 215, 4 GiB RAM, 96 GiB
  disk; Slack uses the deployment responder on port 3083). Its application
  controller owns releases, runtime credentials and systemd. The obsolete
  `june-stage`, `june-source` and `june-provision` resources are intentionally
  removed. They had no delete actions; the command provider removes only their
  bookkeeping, leaving runtime files and processes untouched. Never reintroduce
  the old bootstrap resources from an older branch.
- Phone snapshots are the running releases recovered during reconciliation:
  platform `b9e6c5ddcf2076fcf7674c71be21d5ecd9158b46f9ecd0f6b99100264855c93e`
  and voice `16efef8514ae9fe2684cacac2a9810d666df04862823dd14e00da0ade9899adf`.
  Keep vendored files byte-for-byte intact unless deliberately promoting a new
  application release. The calls-only route remains registered. The voice
  snapshot is copied as an asset, not compiled as infrastructure: its Amp plugin
  targets the phone's installed SDK, so it is excluded from infrastructure tsc.
- `lxcs/artifacts/fetch-bundle.sh` retrieves the pinned, immutable artifacts
  binary from LXC 216 using its enrolled SSH key and verifies SHA-256. It does
  not follow `current` or execute the binary. Run it before a local preview as
  CI does; Pulumi CLI authentication must already be available. The LXC must be
  reachable. An offline artifacts host fails preparation rather than silently
  removing its resources. Keep a separate backup of the binary for disaster
  recovery; this download is not a backup strategy.
- CPA Manager, artifacts, Codex API and Consul's placement on Tower are declared
  in main so an ordinary update preserves them rather than undoing targeted
  deployments made from other worktrees.
  CPA's `codex.mrow.tech` also requires the dashboard-managed Optiplex tunnel
  `a94c53ea-06e2-47ec-bfba-77bb0a5db5a6` to have an exact-host ingress entry
  pointing to `http://192.168.0.203` before the `http_status:404` fallback.
  DNS alone is insufficient: the existing wildcard ingress rules cover only
  `raygen.dev` and `bagelindustries.com`. Preserve those rules when editing the
  tunnel; its ingress configuration is not currently managed by this stack.

Service secrets are read from Infisical. The reconciliation migrated June's
existing credentials into `/june` without rotating them and removed encrypted
service values from the current stack YAML. Bootstrap configuration still comes
from the Infisical-backed `default/homelab` ESC environment.

The previously verified phone host key had reached only some command records.
The reconciliation corrected `connection.hostKey` in the inputs/outputs of
`huddle-phone-environment`, `huddle-phone-deploy`, and `phone-voice-setup` after
an authenticated SSH check. No command was executed for that trust-only state
correction; replaying provisioning would have restarted unrelated applications.
