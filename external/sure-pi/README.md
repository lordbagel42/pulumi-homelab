# Sure on mrow (Raspberry Pi)

**Not deployed or runtime-tested.** On 2026-11-02, TCP/TLS connection attempts
from `homelab-amp` to the API VIP `192.168.10.1:6443` and control planes
`192.168.10.11/12/13:6443` timed out. This does not establish the cause of the
outage. No cluster repair or Optiplex change was attempted.

This is an independent evaluation instance in namespace `sure-pi`, not a move
of the live Optiplex instance. It has its own database, Redis, uploads and
Infisical folder. Nothing imports this directory into the root Pulumi program;
it neither updates nor acquires the shared `homelab` stack. No DNS, tunnel,
public ingress, shared operator, or Optiplex resources are changed.

The machine/platform sources are
[`talos-things`](https://github.com/lordbagel42/talos-things) and
[`mrow-gitops`](https://github.com/lordbagel42/mrow-gitops). The latter normally
owns in-cluster workloads through Flux. These files are deliberately staged in
`pulumi-homelab` as requested, **not automatically reconciled by Flux**. Before
promoting this evaluation to a permanent service, choose one owner: adopt the
release and prerequisites into Flux, or continue explicit Helm management;
never let both manage the same resources.

## Shape and prerequisites

- Helm >= 3.10, Kubernetes >= 1.25, Python 3, Infisical CLI, and an explicit
  authenticated mrow kubeconfig context. The sync helper rejects other API URLs.
- Existing cluster-wide CloudNativePG operator (`mrow-gitops` pins chart 0.29.0)
  and its Cluster CRD. The Sure chart's bundled operators are disabled.
- Existing `local-path` StorageClass for the dedicated 8 GiB Postgres and 1 GiB
  Redis volumes, and `juicefs` for the shared 5 GiB RWX uploads volume. Both web
  and Sidekiq mount `/rails/storage`; pod-local uploads would be lost on restart
  and invisible to the other workload.
- One web process, one Sidekiq worker (all upstream priority/AI queues), one
  Postgres instance and one AOF-persistent Redis. About 1.31 GiB requested
  memory, excluding existing platform overhead; actual fit is unverified.
  The web migration initContainer replaces its app container during startup
  rather than adding another steady-state memory reservation.
- Single-instance local-path DB/Redis are **not HA**; a lost Pi/disk can lose
  data. No Sure-specific off-cluster database backup is configured. Do not put
  irreplaceable finance data here until backup/restore has been exercised.
  Back up uploads as well as Postgres. PVC retention is not a backup.

## ARM64 evidence and pins

The official [Helm chart](https://docs.sure.am/self-hosting-helm) is pinned to
`0.7.6`, matching the latest stable Sure release at inspection. Registry image
indexes were inspected directly with `docker manifest inspect`; all three
configured images have native `linux/arm64` manifests. The config pins those
**ARM64 manifest digests**, so it intentionally cannot run on AMD64:

| Workload               | Image tag                              | ARM64 digest                                                              |
| ---------------------- | -------------------------------------- | ------------------------------------------------------------------------- |
| Web, worker, migration | `ghcr.io/we-promise/sure:0.7.6`        | `sha256:faf7116f462318d63cc9c2b778cefadcd5b14dab5a75b0bfac42a82a0f87afc8` |
| Database               | `ghcr.io/cloudnative-pg/postgresql:16` | `sha256:f7f6d8e7cac77e4a1f7e6acb06b261f7f26ef1928315ff3524db52f970014aee` |
| Queue                  | `redis:7.4-alpine`                     | `sha256:c1945f0eeb2d503f8421952da953a3804d38e4f004d4ef11d57826da59d2c332` |

Sure's v0.7.6 Dockerfile installs native Ruby gems, libvips, PostgreSQL client,
poppler and jemalloc as part of its multi-architecture build. Its entrypoint
selects jemalloc by `uname -m`, not an x86-only path. This is image/build
compatibility evidence, **not a successful boot on the Pis**. Existing CNPG,
JuiceFS and local-path platform health/ARM64 operation still need live checks.
The chart's optional Helm test uses upstream `busybox:1.36`, which also has an
ARM64 manifest; that test-only tag is controlled by the upstream chart.

Offline checks passed: strict Helm lint and rendering of the published 0.7.6
chart, Kubernetes built-in and CNPG CRD schema validation, rendered environment
and volume wiring checks, Ruff/Prettier, repository TypeScript checks/tests,
and synthetic secret-helper success/rejection/leakage checks. No Kubernetes
server-side dry-run or ARM64 runtime test was possible.

## Secrets: Infisical only

Create these values privately in project
`923d970a-8e38-48f5-8c38-e7197c1ce36c`, environment `prod`, folder `/sure-pi`.
Do not reuse or rotate the Optiplex instance's secrets. The helper only reads
injected values; it does not create or modify Infisical secrets.

| Infisical name                         | Purpose                                             |
| -------------------------------------- | --------------------------------------------------- |
| `SURE_PI_SECRET_KEY_BASE`              | Stable Rails key, at least 64 random hex characters |
| `SURE_PI_ENCRYPTION_PRIMARY_KEY`       | Dedicated Active Record encryption key              |
| `SURE_PI_ENCRYPTION_DETERMINISTIC_KEY` | Dedicated deterministic encryption key              |
| `SURE_PI_ENCRYPTION_SALT`              | Dedicated encryption derivation salt                |
| `SURE_PI_POSTGRES_PASSWORD`            | Database owner password                             |
| `SURE_PI_REDIS_PASSWORD`               | At least 64 random hex characters, URI-safe         |
| `SURE_PI_HACKCLUB_AI_KEY`              | Hack Club AI bearer key                             |

Keep the Rails/encryption keys with backups: changing them can make existing
encrypted data unreadable. Do not put values in Helm flags, YAML, Pulumi config,
shell history, or logs. `sync-secrets.py` submits them directly on kubectl stdin,
not argv or a temporary file, and suppresses potentially sensitive diagnostics.
Kubernetes stores the resulting Secrets in its own state; restrict RBAC and
protect etcd backups. No new secret values were provisioned during this work.

## AI scope

Sure v0.7.6 supports a custom `OPENAI_URI_BASE` and model through its native
OpenAI provider. This config uses `https://ai.hackclub.com/proxy/v1`, model
`openai/gpt-4.1-mini`, and chat completions (not the Responses API). That model
was present in the live unauthenticated models list with tool and structured
output support. See [Hack Club AI chat docs](https://docs.ai.hackclub.com/api/chat-completions).

Authenticated chat, tool calls and categorization have **not** been exercised.
No Jev Decisions or external-assistant integration is assumed. Native PDF
processing is disabled. Hosted OpenAI vector-store/file APIs and embeddings
are not configured or verified; this is not a claim of full document/RAG
support. SMTP, banking providers and public TLS routing are also unconfigured.

## Install when the cluster is reachable

These commands mutate only the new namespace/release, but run them only after
reviewing the current cluster and coordinating with its operator. Do not run
the root `pulumi up`. Keep Optiplex live; do not copy its database or change its
hostname. Run from the repository root, and set `CTX` to the actual mrow
context (never rely on the current-context default).

```sh
CTX=mrow
kubectl --context "$CTX" config view --minify -o jsonpath='{.clusters[0].cluster.server}'
# Must be https://192.168.10.1:6443. Check actual platform availability/capacity.
kubectl --context "$CTX" --request-timeout=10s get nodes -L kubernetes.io/arch
kubectl --context "$CTX" get sc local-path juicefs
kubectl --context "$CTX" get crd clusters.postgresql.cnpg.io
kubectl --context "$CTX" -n cnpg-system get deployments

kubectl --context "$CTX" create namespace sure-pi --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
infisical run --projectId 923d970a-8e38-48f5-8c38-e7197c1ce36c --env prod --path /sure-pi --silent -- \
  python3 external/sure-pi/sync-secrets.py --context "$CTX"
kubectl --context "$CTX" apply --dry-run=server -f external/sure-pi/prerequisites.yaml
kubectl --context "$CTX" apply -f external/sure-pi/prerequisites.yaml
kubectl --context "$CTX" -n sure-pi wait --for=condition=Ready cluster/sure-db --timeout=10m
kubectl --context "$CTX" -n sure-pi rollout status statefulset/sure-redis --timeout=5m

helm repo add sure https://we-promise.github.io/sure
helm repo update sure
helm pull sure/sure --version 0.7.6 --untar --untardir /tmp/sure-chart
helm lint /tmp/sure-chart/sure -f external/sure-pi/values.yaml
helm upgrade --install sure sure/sure --version 0.7.6 --kube-context "$CTX" \
  --namespace sure-pi -f external/sure-pi/values.yaml --wait --timeout 15m
```

The packaged chart includes its dependencies; do not install their operators.
Choose an unused temporary chart directory if `/tmp/sure-chart` already exists.
Offline rendering also works with `helm template sure /tmp/sure-chart/sure
-n sure-pi -f external/sure-pi/values.yaml` and requires no credentials.

Migrations run as the web initContainer with `db:prepare`, including a fresh
database. The stock post-install hook is disabled to avoid a `--wait` startup
deadlock, and the web command bypasses the image's second automatic migration.
Sidekiq may restart while the initial schema is prepared. For upgrades, stop
the worker before applying migrations, back up first, and restart it only once
the web rollout is healthy. An image rollback is not a database rollback.

## Live acceptance checks (still outstanding)

```sh
kubectl --context "$CTX" -n sure-pi get pods,pvc
kubectl --context "$CTX" -n sure-pi rollout status deployment/sure-web --timeout=15m
kubectl --context "$CTX" -n sure-pi rollout status deployment/sure-worker --timeout=10m
helm test sure --kube-context "$CTX" -n sure-pi --timeout 5m
kubectl --context "$CTX" -n sure-pi exec deployment/sure-web -- \
  bundle exec rails runner 'abort unless ActiveRecord::Base.connection.select_value("SELECT 1") == 1'
kubectl --context "$CTX" -n sure-pi port-forward --address 127.0.0.1 svc/sure 8080:80
```

Open `http://127.0.0.1:8080/up`, then complete the first-admin flow over the
private port-forward. Signups start open because Sure's invite-only mode also
requires an invite for the very first user. Immediately set **Settings >
Self-Hosting > Onboarding > Closed** after creating the admin, then verify a
second uninvited registration is blocked before exposing the service.
Exercise an ordinary job and a text chat through Hack Club AI, then upload a
file and verify it survives web/worker restarts and is visible to both. Do not
paste financial records, tokens or unredacted application logs into a PR.
Before exposing anything publicly, configure HTTPS and restore
`RAILS_FORCE_SSL`/`RAILS_ASSUME_SSL` appropriately.

For secret updates, sync from Infisical again and restart the web/worker to
pick up the environment changes. PostgreSQL password rotation must also be
coordinated with CNPG; updating a bootstrap secret alone is not a rotation
procedure. Do not delete the namespace/PVCs as a routine rollback.
