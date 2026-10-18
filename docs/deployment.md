# Automatic infrastructure deployment

Pushes to `main` run `.github/workflows/deploy.yml`. Lint must pass before the
hosted runner joins Netbird and runs Pulumi with refresh enabled. Preview uses
the same resource exclusions and artifact preparation as deployment.

## Offline backup server

Inspiron is offline. CI explicitly excludes `pbs-*` and
`proxmox-backup-server` resources from both refresh and update. Their definitions
and state remain intact; CI success does **not** mean backups are healthy. Do not
disable refresh globally or remove these resources to work around the outage.
After Inspiron is recovered, review a PBS-scoped refresh/preview before removing
the exclusions from both workflows.

## Application ownership and immutable inputs

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

Service secrets are read from Infisical. The reconciliation migrated June's
existing credentials into `/june` without rotating them and removed encrypted
service values from the current stack YAML. Bootstrap configuration still comes
from the Infisical-backed `default/homelab` ESC environment.

The previously verified phone host key had reached only some command records.
The reconciliation corrected `connection.hostKey` in the inputs/outputs of
`huddle-phone-environment`, `huddle-phone-deploy`, and `phone-voice-setup` after
an authenticated SSH check. No command was executed for that trust-only state
correction; replaying provisioning would have restarted unrelated applications.
