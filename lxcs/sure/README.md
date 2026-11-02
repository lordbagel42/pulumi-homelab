# Sure on Optiplex

`https://sure.raygen.dev` routes through the existing Cloudflare Tunnel and
Traefik to LXC 219 (`192.168.0.219`, Optiplex). The container has 2 CPU cores,
a 2-core quota, 2 GiB RAM, 512 MiB swap and a 24 GiB disk on `hdd`, avoiding
the nearly-full `local-lvm` pool. Cloudflare Access allows only `sure:ownerEmail`.
The LAN backend remains reachable directly; Access is an internet-edge gate.

Docker Compose runs Sure 0.7.6 web and Sidekiq processes, PostgreSQL 16 and
Redis 7. Database, uploads and Redis data use persistent named volumes. No
database/Redis ports are published. Rails assumes HTTPS behind the tunnel.
The guest pins public DNS because it cannot use the Proxmox host's NetBird
resolver; `.pve-ignore.resolv.conf` preserves that setting over guest restarts.
The 768 MiB worker limit is for ordinary usage, not the memory-intensive demo
generator. SMTP and bank-sync providers are not configured by this deployment.

## Secrets and AI

All secrets live in Infisical project **pulumi-homelab**
(`923d970a-8e38-48f5-8c38-e7197c1ce36c`), environment `prod`, folder `/sure`.
Pulumi generates `SURE_ROOT_PASSWORD`, `SURE_SECRET_KEY_BASE`,
`SURE_POSTGRES_PASSWORD`, `SURE_ENCRYPTION_PRIMARY_KEY`,
`SURE_ENCRYPTION_DETERMINISTIC_KEY` and `SURE_ENCRYPTION_KEY_DERIVATION_SALT`.
The remote provisioner writes a root-only `/opt/sure/.env`; command logging is
disabled and secret inputs/outputs stay secret in Pulumi state. Do not rotate
the encryption keys or database password independently of the stored data.

Supply the existing Hack Club AI key as `SURE_HACKCLUB_AI_KEY` before enabling
`sure:aiEnabled`. The same credential reaches both web and worker. The LLM
endpoint is `https://ai.hackclub.com/proxy/v1`, model `openai/gpt-4.1-mini`,
using Chat Completions rather than the Responses API.

Live synthetic probes on November 2, 2026 returned HTTP 200 from both chat
completions and Jev. Jev resolved `jev-latest` to `jev-1.14.0` and returned the
expected Groceries choice with confidence 1.0. This account has beta access;
`sure:aiEnabled` and `sure:jevEnabled` are enabled. No LLM fallback is needed.

Hack Club AI's source has a dedicated TypeSafe-compatible Jev route at
`https://ai.hackclub.com/proxy/v1/jev/systemone`, model `jev-latest`.
This is not the chat-completions endpoint or OpenRouter's Decisions URL.
It forwards Sure's `state`/`questions` request format unchanged to TypeSafe,
but requires the account's `enable_jev` closed-beta flag. Only set
`sure:jevEnabled` after a live authenticated request returns a valid decision.
If that endpoint denies access, leave `sure:jevEnabled=false` to categorize
using the configured Hack Club AI LLM instead. Without an AI key, neither
cloud path works; Sure's local Bayes categorizer remains available once it has
enough categorized history. Cloud AI sends transaction descriptions to Hack
Club AI and the upstream provider.

## First use and verification

Sign in through Cloudflare Access, then create the first Sure account. Sure
assigns its first account super-admin. Close further signups in Self-Hosting
settings after onboarding if desired. No initial account or financial data is
seeded by infrastructure provisioning.

Verify `/up`, PostgreSQL readiness, Redis PING, Sidekiq process heartbeats and
the AI queue subscriptions. When enabling AI, test a synthetic categorization
from both web and worker; a successful model listing alone is not sufficient.
Use System Health → AI to check the web process and queue worker verification.
Do not send real financial records as deployment fixtures.

Deployment verification ran Sure's actual Jev and OpenAI categorizer code in
both containers: synthetic grocery purchases and bus fares produced the correct,
distinct categories, and Jev confidence cleared 0.7. A queued
`WorkerAiHealthCheckJob` returned from the Sidekiq process with LLM `passing`
and function calling `supported`. The optional vector-store probe returned
`failing` (the job reported `timeout`/`request_failed` diagnostics); vault
document search is not configured or verified by this deployment. This is not
a claim that every AI status probe passes. No account or financial fixtures
were persisted by these checks. The initial schema load exceeded the original
health grace; it completed without a database reset, and the provisioner now
allows a ten-minute cold-start grace. The final Sure-only preview was unchanged.

## Safe updates and source/state reconciliation

Use the `homelab` stack, review a preview, and apply **only Sure targets**:

```sh
pulumi preview --target '**sure**' --target '**SURE**' --diff
pulumi up --target '**sure**' --target '**SURE**'
```

Both patterns are needed: `sure` matches the service and `SURE` matches its
Infisical resource names. Double stars match the complete Pulumi URN. Do not
use a whole-stack apply from incomplete source or disable resource protection.

At deployment time, `origin/main` (`e4259bb`) lacked the live `amp-orbs`
definition. Its owning source is the untracked
`/home/amp/workspaces/pulumi-homelab/lxcs/amp-orbs/` directory in the original,
diverged checkout. This branch owns `lxcs/sure/`; it does not adopt or overwrite
that other service's local work. Preserve the original checkout and its config.

Before any merge that triggers a full-stack deployment, reconcile **both**
owning modules and their non-secret enable/host-key settings into a candidate
based on current main. Have the amp-orbs owner preserve/review its source; do
not replace it with a stub or copy state inputs into source. Preview the
combined candidate against live state and confirm there are no protected
deletions or unrelated updates. Because merging either incomplete branch
triggers deployment, coordinate an authorized deployment hold or land a
combined reconciled candidate; do not simply merge one and hope the next
merge arrives first. Then rebase other homelab branches on that reconciled
main. Kubernetes/Helm configuration belongs in `lordbagel42/talos-things`,
not this repository or stack.

## Cloudflare Workers assessment

**No, not as an ordinary Worker deployment.** Sure requires a Ruby/Rails
runtime, persistent PostgreSQL and Redis, a long-running Sidekiq process and
upload storage. Workers' web/JavaScript runtime cannot run this Docker Compose
stack unchanged. Cloudflare Containers with external persistent services would
be a different architecture, not a simple Worker host; no such port is built.
Cloudflare Tunnel/Access is suitable as the edge in front of this LXC.

Sources: [Sure Docker hosting](https://docs.sure.am/self-hosting),
[Sure AI configuration](https://docs.sure.am/llm-support),
[Sure Jev](https://docs.sure.am/providers/jev),
[Hack Club AI Jev route](https://github.com/hackclub/ai/blob/main/src/routes/proxy/v1/jev.ts),
[Workers runtime](https://developers.cloudflare.com/workers/runtime-apis/).
