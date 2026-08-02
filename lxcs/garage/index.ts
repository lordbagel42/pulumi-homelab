import * as path from "path";
import * as command from "@pulumi/command";
import * as consul from "@pulumi/consul";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword, managedSecret } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";
import { CONSUL_IP_CONST, CONSUL_VERSION, HOMELAB_DATACENTER } from "../hashistack";

/**
 * Garage — S3-compatible object storage for the homelab.
 *
 * ── Why this exists ─────────────────────────────────────────────────────────
 * The mrow Kubernetes cluster's JuiceFS filesystem stores its chunks in
 * Cloudflare R2. That works and is genuinely offsite, but every read that misses
 * the local cache is a WAN round trip over a domestic uplink, and every byte
 * written leaves the house.
 *
 * This gives JuiceFS a second, LAN-local backend. See the mrow-gitops
 * `platform/juicefs-homelab` component for how it is consumed — and in
 * particular for why it is a SECOND filesystem rather than a second bucket
 * behind the existing one.
 *
 * ── Why Garage and not MinIO ────────────────────────────────────────────────
 * An earlier revision of this used MinIO. Garage instead, for reasons that all
 * point the same way at this scale:
 *
 *   • It is built for exactly this shape — self-hosted, small, commodity
 *     hardware, one operator. MinIO is built for the case where erasure coding
 *     across many drives is the point, and single-node single-drive is the
 *     configuration it warns you about.
 *   • No web console to secure. MinIO's console can mint credentials and delete
 *     buckets, so it needs a Traefik route and an authentik policy in front of
 *     it. Garage has no console at all; it is administered by the `garage` CLI
 *     over SSH, which is one fewer public surface to get wrong.
 *   • Credentials are not length-constrained. MinIO enforces bounds (20/40) that
 *     forced this module to truncate Infisical's generated secrets, with a
 *     failure mode — a server that refuses to boot — that reads nothing like a
 *     length problem. Garage's only constraints are a minimum length and an
 *     ASCII charset, so the generated values go in as-is.
 *
 * The costs are real and worth stating: Garage's S3 surface is narrower than
 * MinIO's, and it has no equivalent of `mc admin`. Neither matters to JuiceFS,
 * which uses PUT/GET/DELETE/list and nothing else.
 *
 * ── Why an LXC and not a Nomad job ──────────────────────────────────────────
 * The obvious alternative was a Nomad job with a host volume on the
 * nomad-client, following the precedent lookout's Postgres sets. Rejected for
 * three reasons:
 *
 *   1. Disk. The nomad-client is a 40 GiB VM already carrying container images
 *      and lookout's Postgres. Object storage backing a distributed filesystem
 *      wants its own disk, not the tail of a shared one.
 *   2. A stable endpoint. JuiceFS mounts point at a fixed address. A Nomad
 *      allocation can be rescheduled, and while a host volume pins the DATA to
 *      one client, the address would still move with the allocation.
 *   3. Failure domain. A chaos experiment or an OOM on the nomad-client should
 *      not be able to take out the storage layer underneath a filesystem
 *      mounted on another cluster entirely.
 *
 * ── Durability, stated plainly ──────────────────────────────────────────────
 * This is Garage with `replication_factor = 1` on one Proxmox host, on one LVM
 * volume, on consumer hardware. There is NO replication and NO erasure coding.
 * It will lose everything if that disk dies.
 *
 * That is acceptable only because of how it is used: it is the second copy, not
 * the only one. R2 remains the durable backend, and the mrow-gitops component
 * that consumes this is explicit that anything which must survive belongs on the
 * R2-backed filesystem. Do not put a sole copy of anything here.
 */
export const name = "garage";
export const provides = ["garage-setup"];
export const dependencies: string[] = ["consul-setup"];

const GARAGE_VMID = 212;
const GARAGE_IP = ip(GARAGE_VMID);

/**
 * Pinned. Garage's config format has changed across majors — `replication_mode`
 * became `replication_factor` in v1.0 — so an unpinned installer could pull a
 * release whose config this playbook no longer writes correctly, on a
 * re-provision, with nothing in git to explain the change.
 */
const GARAGE_VERSION = "v2.3.0";

/** S3 API. What JuiceFS and anything else S3-shaped talks to. */
export const GARAGE_S3_PORT = 3900;
/** Inter-node RPC. Single node, so nothing dials this but the local CLI. */
const GARAGE_RPC_PORT = 3901;
/** Admin API. Health and metrics; also the only management surface. */
const GARAGE_ADMIN_PORT = 3903;

export const GARAGE_ENDPOINT = `http://${GARAGE_IP}:${GARAGE_S3_PORT}`;

/**
 * The S3 region Garage reports. Garage is not regional, but the AWS SDK — and
 * therefore JuiceFS — insists on a region for request signing, and it has to
 * match on both sides or every request fails signature validation. "garage" is
 * the project's own default; the mrow-gitops Secret must say the same.
 */
export const GARAGE_REGION = "garage";

/**
 * The bucket JuiceFS writes into. Named to match the R2 bucket it mirrors in
 * purpose (`mrow-juicefs`) with the backend in the name, because the two are
 * different filesystems and confusing them is the expensive mistake here.
 */
export const JUICEFS_BUCKET = "mrow-juicefs-homelab";

/**
 * Disk for the container, in GiB.
 *
 * This is the binding constraint on how much a homelab-backed JuiceFS volume can
 * hold, and it is the one number here likely to need changing. CHECK FREE SPACE
 * ON THE HOST FIRST — `pvs` / `vgs` on the Proxmox node. For reference, the
 * other guests take 24 (dokploy), 20 (authentik) and 40 (nomad-client).
 *
 * Growing it later is a Proxmox disk resize plus a filesystem grow inside the
 * container; shrinking it is not practical. Start smaller than you think you
 * need.
 */
const GARAGE_DISK_GIB = 100;

/**
 * Capacity advertised to Garage's layout, in GB.
 *
 * Deliberately below GARAGE_DISK_GIB. Garage treats this as the amount of DATA
 * it may place on the node and does not subtract its own metadata, the LMDB map
 * file, or the root filesystem — so advertising the full disk is how a node ends
 * up wedged with a full partition rather than returning a clean quota error.
 *
 * The units differ too, which is the easy mistake: the disk is 100 GiB
 * (~107.4 GB) and this is decimal GB, as Garage's `-c` flag parses it.
 */
const GARAGE_CAPACITY_GB = 85;

export function register(ctx: ServiceContext): void {
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: GARAGE_VMID,
        ip: GARAGE_IP,
        // Garage is not CPU-hungry at this scale, but it hashes and checksums
        // everything it stores. Two cores so a large JuiceFS write does not
        // starve its own health endpoint.
        cpu: 2,
        // 2 GiB. Garage's memory use is dominated by the LMDB page cache and
        // in-flight block buffers rather than by stored bytes, and this serves
        // one JuiceFS mount per node.
        memory: 2048,
        disk: GARAGE_DISK_GIB,
        tags: ["garage", "storage", "app"],
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),

        // ── No reverse proxy, deliberately ────────────────────────────────────
        // Garage has no web console, so unlike the MinIO revision of this module
        // there is nothing to put behind authentik. The three listening ports are
        // all machine interfaces:
        //
        //   3900  S3 API     — LAN only. That is all JuiceFS needs, and routing
        //                      it through the tunnel would put object storage
        //                      holding a filesystem's contents on the public
        //                      internet behind nothing but an access key.
        //   3901  RPC        — single node; nothing dials it but the local CLI.
        //   3903  admin API  — bearer-token authenticated, and the token is full
        //                      control. Reachable on the LAN for Consul's health
        //                      probe; not routed.
        //
        // Administration is `garage` over SSH. If a UI is ever wanted, that is a
        // deliberate decision to make then, with a route and a policy — not a
        // default that arrived because the field was already there.
        consulProvider: ctx.consulProvider,
    }, { provider: ctx.provider });

    // Only non-secret values may go through extraVars — ansibleProvision rejects
    // Outputs outright, because they JSON-encode to an error string.
    const provision = ansibleProvision("garage-provision", {
        host: GARAGE_IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            consul_ip: CONSUL_IP_CONST,
            consul_version: CONSUL_VERSION,
            datacenter: HOMELAB_DATACENTER,
            garage_version: GARAGE_VERSION,
            s3_port: GARAGE_S3_PORT,
            rpc_port: GARAGE_RPC_PORT,
            admin_port: GARAGE_ADMIN_PORT,
            s3_region: GARAGE_REGION,
            juicefs_bucket: JUICEFS_BUCKET,
            capacity_gb: GARAGE_CAPACITY_GB,
        },
        dependsOn: [machine],
    });

    /**
     * Cluster RPC secret. Single node, so this authenticates nothing but the
     * local CLI to the local daemon — but Garage requires it and refuses to
     * start without one, and generating it here rather than in the playbook
     * keeps it stable across re-provisions.
     *
     * Note it is a 64-hex string because Garage requires exactly 32 bytes
     * hex-encoded for this one, unlike the credentials below.
     */
    const rpcSecret = managedSecret("garage-rpc-secret", ctx.infisicalConfig);

    /**
     * Admin API bearer token. Full control over the cluster — it can create and
     * delete buckets and mint keys — so it is treated as a root credential and
     * kept off the LAN except for Consul's unauthenticated health probe.
     */
    const adminToken = managedSecret("garage-admin-token", ctx.infisicalConfig);

    /**
     * A SEPARATE credential for JuiceFS, not the admin token. JuiceFS holds this
     * key in a Kubernetes Secret on another cluster; scoping it to one bucket
     * means a compromise there cannot enumerate or delete anything else here.
     *
     * ── On the shape of these values ─────────────────────────────────────────
     * managedSecret generates `openssl rand -hex 32` — 64 hex characters.
     *
     * Garage's validation is permissive: a key id needs to be at least 8
     * characters of `[A-Za-z0-9._-]`, and a secret at least 16 graphic ASCII
     * characters. So unlike the MinIO revision of this module, NOTHING HERE IS
     * TRUNCATED TO SATISFY A LIMIT — that truncation existed only because MinIO
     * caps credentials at 20/40 and rejects anything longer.
     *
     * The access key is still reshaped, for a different and weaker reason: it is
     * `GK` + 24 hex characters, which is byte-for-byte the format
     * `garage key create` generates itself (`Key::new` in key_table.rs). Garage
     * would accept the raw 64 hex, but matching its native format means a key
     * read out of `garage key list` looks like every other key, and anything
     * pattern-matching the `GK` prefix keeps working. 24 hex is 96 bits, which
     * is ample for an identifier that is not itself a secret.
     *
     * The secret key is passed through in full, all 64 hex characters — the same
     * length Garage generates.
     */
    const juicefsAccessKey = managedSecret("garage-juicefs-access-key", ctx.infisicalConfig)
        .apply((v) => `GK${v.slice(0, 24)}`);
    const juicefsSecretKey = managedSecret("garage-juicefs-secret-key", ctx.infisicalConfig);

    // Written over SSH rather than through extraVars, so the secret Outputs
    // actually resolve. Piped via stdin from the command's environment so no
    // value appears in argv on either end — same shape as authentik.
    //
    // The layout/bucket/key logic deliberately lives in
    // /usr/local/bin/garage-bootstrap.sh, installed by the playbook, rather than
    // inline here. Inlining it would need four levels of shell quoting (template
    // literal -> bash -c -> ssh -> remote heredoc), which is unreviewable. The
    // script reads its inputs from the env files below, so nothing needs
    // escaping at all.
    const credentials = new command.local.Command("garage-credentials", {
        interpreter: ["/bin/bash", "-c"],
        create: `
set -euo pipefail
key=$(mktemp)
chmod 600 "$key"
printf '%s\\n' "$_SSH_KEY" > "$key"
trap 'rm -f "$key"' EXIT
SSH="ssh -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10"
HOST=root@${GARAGE_IP}

# Read by both the systemd unit and the CLI. Garage supports these as env vars
# precisely so they need not sit in garage.toml — which matters because Garage
# refuses to start when a config file holding secrets is world-readable
# (allow_world_readable_secrets defaults to false).
#
# umask 077 because GARAGE_ADMIN_TOKEN is full control over the cluster.
{
  printf 'GARAGE_RPC_SECRET=%s\\n'  "$_RPC_SECRET"
  printf 'GARAGE_ADMIN_TOKEN=%s\\n' "$_ADMIN_TOKEN"
} | $SSH "$HOST" "install -d -m 0750 /etc/garage && umask 077 && cat > /etc/garage/garage.env"

# Separate file from the server's own environment: the server must never need
# the JuiceFS key, and the bootstrap script must never need the admin token in
# order to import it.
{
  printf 'JFS_ACCESS_KEY=%s\\n' "$_JFS_AK"
  printf 'JFS_SECRET_KEY=%s\\n' "$_JFS_SK"
} | $SSH "$HOST" "umask 077 && cat > /etc/garage/juicefs.env"

$SSH "$HOST" "systemctl enable --now garage && systemctl restart garage"

# Idempotent: assigns the layout on first run, creates the bucket if absent, and
# imports or rotates the scoped key. Re-runs on every credential rotation, which
# is the point.
$SSH "$HOST" "/usr/local/bin/garage-bootstrap.sh"
        `.trim(),
        environment: {
            _SSH_KEY: ctx.sshPrivateKey,
            _RPC_SECRET: rpcSecret,
            _ADMIN_TOKEN: adminToken,
            _JFS_AK: juicefsAccessKey,
            _JFS_SK: juicefsSecretKey,
        },
        // Re-run when any credential changes, so a rotation in Infisical
        // actually reaches the server.
        triggers: [rpcSecret, adminToken, juicefsAccessKey, juicefsSecretKey],
    }, { dependsOn: [provision] });

    ctx.commands.set("garage-setup", credentials);

    // ── The S3 API in the Consul catalog ──────────────────────────────────────
    // Registered with NO traefik.enable tag — this is a catalog entry for
    // discovery, not a route. Two things make it worth having:
    //
    //   • It is what makes the S3 endpoint discoverable through the cluster
    //     peering link. mrow's Consul imports homelab services, so a workload
    //     there can resolve this rather than hardcoding 192.168.0.212.
    //   • Garage's admin health endpoint gives Consul a real check, so a Garage
    //     that is running but not serving is visible as critical rather than as
    //     a mystery on the JuiceFS side.
    if (ctx.consulProvider) {
        const node = new consul.Node("garage-s3-node", {
            name: "garage-s3-svc",
            address: GARAGE_IP,
        }, { provider: ctx.consulProvider, dependsOn: [credentials] });

        new consul.Service("garage-s3-service", {
            name: "garage-s3",
            node: node.name,
            address: GARAGE_IP,
            port: GARAGE_S3_PORT,
            // No traefik.* tags. Deliberate — see above.
            tags: ["s3", "storage", "juicefs"],
            checks: [{
                checkId: "garage-s3-health",
                name: "Garage S3 API",
                // Initial state before the first probe runs. Critical rather than
                // passing, so a service that never becomes healthy is never
                // briefly advertised as good.
                status: "critical",
                // The ADMIN port, not the S3 port. Garage's S3 endpoint has no
                // unauthenticated health path — an anonymous GET / returns a
                // signature error, which is a 403 and would read as permanently
                // critical. /health on the admin API is unauthenticated by
                // design and returns 503 when the cluster cannot serve requests,
                // which is exactly the question being asked.
                http: `http://${GARAGE_IP}:${GARAGE_ADMIN_PORT}/health`,
                interval: "30s",
                timeout: "5s",
                // ── Load-bearing, and NOT what the provider defaults to ────────
                // Left unset, this provider sends `deregister_critical_service_after
                // = 30s`. That tells Consul to DELETE the service registration
                // once the check has been critical for 30 seconds — so a Garage
                // restart lasting slightly longer than one probe interval removes
                // it from the catalog entirely.
                //
                // That is bad in a way that compounds: the registration is managed
                // by Pulumi, so Consul deleting it creates drift that nothing
                // repairs until the next `pulumi up`. The service would stay
                // missing from the catalog long after Garage came back, and the
                // symptom — "Garage is running but not in Consul" — points nowhere
                // near a health-check setting.
                //
                // 72h instead: a genuinely dead registration still gets cleaned
                // up eventually, and a restart or a brief network blip does not.
                // The check still goes critical immediately either way, which is
                // what anything reading health actually looks at.
                deregisterCriticalServiceAfter: "72h",
            }],
        }, { provider: ctx.consulProvider, dependsOn: [node] });
    }
}
