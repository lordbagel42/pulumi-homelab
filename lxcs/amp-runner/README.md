# Amp runner

Unprivileged Debian 13 LXC **214**, `192.168.0.214`, on **tower**. Replaces the
removed `sandbox-next` LXC 232 and its Claude/MCP gateway services.

## Capacity and limits

- **3 CPU cores**, hard quota **3**, CPU weight **25** (normal cgroup v2 weight is 100).
- **12 GiB RAM**, **no swap**, **96 GiB disk** on `local-lvm`.
- Amp and its subprocesses: memory reclaim above **10 GiB**, hard **11 GiB**
  limit, **2,048 tasks**, nice **10**, low-priority disk I/O.
- Starts on boot; Pulumi protects the container from accidental deletion.

Tower has four physical cores and about 15.1 GiB usable RAM. These limits leave
one core-equivalent and about 3 GiB outside the container, plus 1 GiB inside it
for administration. They assume the old 12 GiB sandbox is **removed**. Do not
restore it alongside this container without re-budgeting RAM. The application
host (OptiPlex) was already using about 13 GiB and swapping when this was sized.

The disk is a mechanical HDD, so package installs will be slower than on an SSD.
I/O priority is not a bandwidth guarantee. All runner threads share these limits
and the `amp` Unix account; this is not per-thread isolation. An LXC also shares
the host kernel and is not a security boundary for hostile code.

## First login

The provisioning playbook installs Amp from its official installer, Git, GitHub
CLI, Debian's Node.js/npm, pnpm 10.33.0, Python/venv, build tools, and common CLI
utilities. It does not copy desktop credentials or configure GitHub credentials.
The `amp` account has passwordless sudo for all commands inside the container;
agents running under this account can therefore execute commands as root.
Password login remains locked; use SSH-key authentication.

SSH uses the public key in `proxmox-pulumi.pub`. Sign in as `amp` using its
matching private key:

```sh
ssh -t -i ~/.ssh/proxmox-pulumi amp@192.168.0.214 \
  /home/amp/.amp/bin/amp login
```

Open the link it prints and finish the login. Then, as root on the container:

```sh
systemctl start amp-runner
systemctl status amp-runner
journalctl -u amp-runner -f
```

The enabled service waits for `/home/amp/.local/share/amp/secrets.json` before
starting. After first login and startup, it restarts on failure and starts on
subsequent boots. Amp manages its own idle-time CLI updates.

Choose **homelab-amp** in Amp's runner picker. The initial directory is
`/home/amp/workspaces`. Clone repositories beneath it; Git checkouts and worktrees
are discovered automatically. Terminal access is enabled. Amp-hosted environment
variable injection is intentionally not enabled by default.

## Verify or reprovision

```sh
node --test framework/proxmox-machine.test.cjs
pnpm exec tsc --noEmit
ansible-playbook -i localhost, --syntax-check lxcs/amp-runner/playbook.yml
```

Preview before deploying. For a targeted update, use exact URNs for the
`amp-runner` component, its `ContainerLegacy` child, and `amp-runner-provision`.
On first creation, enroll the guest SSH host key through `pct exec 214` on Tower
before provisioning it. The shared node template is retained from the old sandbox.
Do not apply an unreviewed whole-stack update: this checkout may contain unrelated
pending changes.
