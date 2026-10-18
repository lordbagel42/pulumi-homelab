# Amp runner

Privileged Debian 13 LXC **214**, `192.168.0.214`, on **tower**. Replaces the
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

The service starts without `--share`, making **homelab-amp** private to the
signed-in account. It uses `--amp-env` to inject personal, project, and workspace
Secrets & Env Vars into thread shells, MCP servers, and plugins. Shared runners
exclude personal secrets, including in the owner's threads; do not add `--share`
without approval to change access and secret scope.

Choose **homelab-amp** in Amp's runner picker. The initial directory is
`/home/amp/workspaces`. Clone repositories beneath it; Git checkouts and worktrees
are discovered automatically. Terminal access and Amp-hosted environment
variable injection are enabled.

Sharing changes take effect when the runner starts; changing `--share` in the
unit does not affect an already-running process. The playbook reloads systemd
configuration but never restarts an active runner. Leave current threads running and activate
the change at a separately approved service restart after they finish. Amp's
in-process updates preserve the old command-line flags, so they are not a
substitute for a service restart.

## Global guidance and browser automation

The playbook manages `/home/amp/.config/amp/AGENTS.md`, which Amp automatically
includes in runner sessions regardless of the working repository. It preserves
the sudo, repository-bootstrap, and homelab guidance and documents browser use.
Edit the inline content in `playbook.yml`, not just the live file. Keeping the
content in the playbook makes `ansibleProvision`'s SHA-256 trigger detect changes;
the next Pulumi update of `amp-runner-provision` reapplies them.

`agent-browser` **0.38.1** is installed in `/usr/local/bin` from its official
Linux x64 release with a pinned SHA-256 digest. This is the standalone Rust
binary, so it does not require upgrading Debian's Node.js to meet the npm
package's Node 24 requirement. Update the release URL and checksum together;
do not use `agent-browser upgrade` for this managed installation.

Ansible installs the Debian 13 browser libraries and fonts, then runs
`agent-browser install` as `amp`. This downloads the current Chrome for Testing
Stable release into `/home/amp/.agent-browser/browsers`, reusing that version if
already cached. Chrome is not pinned to the CLI version; a later provisioning
run can download a newer Stable release.

This provides headless browsing through compact accessibility snapshots, without
a Playwright MCP server. It is **not** connected to the user's desktop browser
or existing logins. Use a unique session name per thread on every command:

```sh
agent-browser --session my-thread open https://example.com
agent-browser --session my-thread snapshot -i -c
agent-browser --session my-thread screenshot /home/amp/workspaces/example.png
agent-browser --session my-thread close
```

Use refs from the current snapshot for `click`/`fill`; re-snapshot after page
changes. Inspect screenshots rather than merely capturing them. Close only your
own session, and do not expose debugging ports or run global cleanup on this
shared runner. Browser actions still require the same authorization as other
external actions.

## Verify or reprovision

```sh
node --test framework/proxmox-machine.test.cjs
pnpm exec tsc --noEmit
ansible-playbook -i localhost, --syntax-check lxcs/amp-runner/playbook.yml
```

To stage **only the runner service configuration** without restarting an active
runner, use `--tags runner-service` with the same local commands below. This
updates the unit, reloads systemd, and ensures the service is enabled and started;
it does not restart a running service or change journald, guidance, or browsers.
Record `systemctl show amp-runner -p MainPID -p ExecMainStartTimestamp -p NRestarts`
before and after applying it to verify process continuity.

To preview and apply **only guidance and browser setup** on this runner, from
the repository root (requires Ansible on the invoking machine):

```sh
ansible-playbook -i localhost, -c local -e ansible_python_interpreter=/usr/bin/python3 \
  lxcs/amp-runner/playbook.yml --tags runner-environment --check --diff
ansible-playbook -i localhost, -c local -e ansible_python_interpreter=/usr/bin/python3 \
  lxcs/amp-runner/playbook.yml --tags runner-environment
```

The tag does not touch sudoers, the runner service, or journald and does not
restart any services. It assumes the initial account/sudo setup already exists.
Re-run it to verify idempotence (zero changes while Chrome Stable is unchanged),
then check `agent-browser --version`, the session workflow above, and
`sudo -n -k whoami` (expected: `root`). The browser download is skipped in check
mode, so a dry run alone does not verify browser installation.

Preview before deploying. For a targeted update, use exact URNs for the
`amp-runner` component, its `ContainerLegacy` child, and `amp-runner-provision`.
On first creation, enroll the guest SSH host key through `pct exec 214` on Tower
before provisioning it. The shared node template is retained from the old sandbox.
Do not apply an unreviewed whole-stack update: this checkout may contain unrelated
pending changes.
