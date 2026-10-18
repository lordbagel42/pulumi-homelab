# CPA Manager Plus

Dedicated unprivileged Debian 13 LXC **217** on **optiplex**, with one CPU
(one-core quota), 1024 MiB RAM, 256 MiB swap, and **12 GiB on local-lvm (SSD)**.
Pulumi protects the container against replacement/deletion. Both Docker services
restart automatically. Public HTTPS uses the existing homelab Cloudflare tunnel
and Traefik, with a dedicated DNS record and Consul catalog registration.

- Manager: `https://codex.mrow.tech/management.html`
- OpenAI-compatible gateway: `https://codex.mrow.tech/v1`
- LAN fallback: manager `http://192.168.0.217:18317`, gateway `http://192.168.0.217:8317`.
- Images: CPA Manager Plus 1.14.7 and CLIProxyAPI v8.0.32, pinned by digest.

The manager uses its Admin Key and API clients use the separate Client Key.
Traefik routes only `/v1` and `/v1/…` to the gateway; other paths reach the manager.
There is no interactive SSO middleware in front of API requests. The previous
Cloudflare `sub2api` Worker, custom domain, Durable Object namespace, and R2 bucket
were deleted with owner authorization before this hostname was reassigned.

## Credentials and state

Secrets are in Infisical project `923d970a-8e38-48f5-8c38-e7197c1ce36c`,
environment `prod`, folder `/cpa-manager`:

| Secret                      | Purpose                                                     |
| --------------------------- | ----------------------------------------------------------- |
| `CPA_MANAGER_ADMIN_KEY`     | Manager browser login                                       |
| `CPA_MANAGER_CLIENT_KEY`    | Gateway client API key                                      |
| `CPA_MANAGER_UPSTREAM_KEY`  | Manager-to-gateway management authentication                |
| `CPA_MANAGER_DATA_KEY`      | Stable encryption-key input; preserve with database backups |
| `CPA_MANAGER_ROOT_PASSWORD` | Guest root password; SSH normally uses the stack key        |

Provisioning injects root-only runtime files under `/opt/cpa-manager/secrets`.
The manager's upstream connection is environment-managed. No AI provider accounts
are preconfigured. Provider secrets should be sourced from Infisical, not Git or
Pulumi config. Store new provider credentials there before configuring the service.

`/opt/cpa-manager/data` holds SQLite state; `cpa/config.yaml`, `cpa/auths`, and
`cpa/logs` persist the gateway configuration and accounts. Provisioning seeds CPA
configuration only when absent, preserving subsequent UI changes. Changing an
Infisical key alone does not rotate an existing manager login or CPA config:
follow upstream's key-rotation procedure and refresh the corresponding Pulumi
secret-fetch resource. Never change the data key for an existing database blindly.

The import temporary-file quota is **10 GiB** (`10737418240` bytes), with one
concurrent import and a 60-minute session TTL. This is not reserved disk space,
nor a total database limit. A full-quota import may exhaust the 12 GiB filesystem
because processing, SQLite/WAL, images, and the OS need additional space. Usage
history grows independently. Docker logs are capped at 10 MiB per service, and
journald at 32 MiB persistent / 16 MiB runtime. Monitor free space before imports.

## Deploy and verify

Set `cpa-manager:enabled=true`. Without `cpa-manager:sshHostKey`, the module creates
the LXC, routing, and its Infisical root credential but does not provision Docker.
Verify VMID/IP availability first; target only container/root-secret resources
for initial creation and include routing after application health checks.
Enroll the public guest SSH key through trusted Optiplex access:

```sh
pct exec 217 -- cat /etc/ssh/ssh_host_ed25519_key.pub
```

Then set `cpa-manager:sshHostKey` and preview/apply **only** these resource URNs
(substitute the stack/project if different):

```text
urn:pulumi:homelab::pulumi-homelab::command:local:Command::infisical-secret-CPA_MANAGER_*
urn:pulumi:homelab::pulumi-homelab::homelab:framework:ProxmoxMachine::cpa-manager
urn:pulumi:homelab::pulumi-homelab::homelab:framework:ProxmoxMachine$proxmoxve:index/containerLegacy:ContainerLegacy::cpa-manager
urn:pulumi:homelab::pulumi-homelab::homelab:framework:ProxmoxMachine$command:remote:Command::cpa-manager-provision
urn:pulumi:homelab::pulumi-homelab::cloudflare:index/dnsRecord:DnsRecord::cpa-manager-dns
urn:pulumi:homelab::pulumi-homelab::homelab:framework:ProxmoxMachine$consul:index/node:Node::cpa-manager-node
urn:pulumi:homelab::pulumi-homelab::homelab:framework:ProxmoxMachine$consul:index/service:Service::cpa-manager-service
```

Use each as a quoted `--target`; do not target the whole stack or dependents.
The October 18 deployment needed a private `--config-file` copy with
`ai-proxy:enabled=false` because that unrelated module hashes an undefined output
during targeted updates. This temporary override does not disable the running
AI proxy when using only the exact CPA targets above. The real stack config
retains AI proxy enabled; do not use that temporary config for a full-stack apply.

Checks inside the guest:

```sh
cd /opt/cpa-manager
docker compose ps
df -h /
curl -f http://192.168.0.217:18317/health
docker compose exec -T cpa-manager-plus printenv USAGE_IMPORT_DISK_QUOTA_BYTES
```

`/status` requires the Manager Admin Key; verify its collector is running and
`databaseMaintenance.required` is false. Gateway `/v1/models` requires the client
key and remains empty until providers are configured. Check browser login too.
Only the LAN address is published, not the guest's loopback address.

Before upgrades, stop both services and securely back up all of
`/opt/cpa-manager` along with the matching Infisical secrets; restart afterward.
No automatic backup job is created. Never prune active data to make an upgrade fit.
Local validation: `pnpm test` and Python syntax checks; live deployment is the
integration check for Debian packages, Compose, nested Docker, and health.
