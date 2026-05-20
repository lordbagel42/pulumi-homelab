# Adding a Service

Services live under `lxcs/` (containers) or `vms/` (full VMs). The framework
auto-discovers every subdirectory that contains either a `service.yaml` or an
`index.ts` with a `register` export — no changes to `index.ts` or any other
central file are needed.

---

## IP / VMID convention

Every static resource follows the rule: **IP last-octet = VMID**.

| Range | Purpose | Examples |
|-------|---------|---------|
| 100–199 | DHCP / ephemeral VMs | PBS: 100 |
| 201–210 | System LXCs | consul: 201, nomad: 202, traefik: 203, cloudflared: 204 |
| 211–229 | App LXCs | dokploy: 211, authentik: 220 |
| 230–239 | Static VMs | nomad-client: 230 |
| 240–255 | Reserved | — |

Pick an unused ID and the IP follows automatically: `ip(251)` → `192.168.0.251`.

---

## Option A — YAML (simplest, no TypeScript required)

Best for: a standard LXC + a setup script + optional Traefik reverse proxy.

**1. Create a directory**

```
lxcs/my-service/
  service.yaml
  setup.sh
```

**2. Write `service.yaml`**

```yaml
name: my-service          # Consul service name + Pulumi resource prefix
vmid: 251                 # unique — check the table above
ip: 192.168.0.251         # must match vmid

# Resources (defaults shown — all optional)
cpu: 1
memory: 512               # MB
disk: 8                   # GB
tags: [app]

# Set true if the container needs to run Docker
nesting: false
privileged: false

# Setup script run via SSH after the container starts
setup:
  script: setup.sh
  env:
    CONSUL_IP: "192.168.0.201"
    MY_VAR: "some-value"

# Traefik reverse proxy (optional)
# The framework writes the Consul service JSON automatically.
reverseProxy:
  domain: my-service.bagelindustries.com
  port: 8080
  protected: true    # false = public, true = require Authentik login
  entrypoint: web

# Block setup until these commands have finished (optional)
dependsOn:
  - consul-setup
```

**3. Write `setup.sh`**

Standard Debian-based shell script. Env vars listed under `setup.env` are
exported into the shell's environment before the script runs.

```bash
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y -qq
apt-get install -y -qq curl

# CONSUL_IP and MY_VAR are available here from service.yaml
echo "Consul is at $CONSUL_IP"
```

**That's it.** Push the branch; the next `pulumi up` will create the container,
wait for SSH, run `setup.sh`, and (if `reverseProxy` is set) register the
Consul service with the right Traefik tags.

---

## Option B — TypeScript module

Best for: services with multiple containers, Pulumi secrets (Infisical), custom
SSH commands, or dependencies on other services' outputs.

**1. Create a directory**

```
lxcs/my-service/
  index.ts
  setup.sh          # optional — you wire it yourself
```

**2. Write `index.ts`**

Export exactly these four things:

```typescript
import * as proxmox from "@muhlba91/pulumi-proxmoxve";
import { ip, GATEWAY } from "../../framework";
import { lxcPassword } from "../../infisical";
import { sshSetup } from "../../utils/ssh";
import type { ServiceContext } from "../../framework";
import * as path from "path";

const VMID = 251;
const IP   = ip(VMID);

// ── ServiceModule contract ────────────────────────────────────────────────────
export const name         = "my-service";
export const provides     = ["my-service-setup"];   // command names this module registers
export const dependencies: string[] = [];           // command names to wait for

export function register(ctx: ServiceContext): void {
    const container = new proxmox.ContainerLegacy("my-service", {
        nodeName: "optiplex",
        vmId: VMID,
        cpu: { cores: 1 },
        memory: { dedicated: 512 },
        disk: { datastoreId: "local-lvm", size: 8 },
        operatingSystem: {
            templateFileId: "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
            type: "debian",
        },
        networkInterfaces: [{ name: "veth0", bridge: "vmbr0" }],
        initialization: {
            hostname: "my-service",
            ipConfigs: [{ ipv4: { address: `${IP}/24`, gateway: GATEWAY } }],
            userAccount: { password: lxcPassword("my-service", ctx.infisicalConfig), keys: [ctx.sshKey] },
        },
        tags: ["app"],
        startOnBoot: true,
        started: true,
    }, { provider: ctx.provider });

    const setup = sshSetup(
        "my-service-setup",
        IP,
        path.join(__dirname, "setup.sh"),
        { CONSUL_IP: "192.168.0.201" },
        ctx.sshPrivateKey,
        [container],
    );

    // Register so other modules can depend on this
    ctx.commands.set("my-service-setup", setup);
}
```

### Dependency on another service

Declare the command name in `dependencies` and pull it from `ctx.commands`
inside `register`:

```typescript
export const dependencies = ["consul-setup"];

export function register(ctx: ServiceContext): void {
    const consulDone = ctx.commands.get("consul-setup");
    // pass it in dependsOn:
    const setup = sshSetup("...", IP, scriptPath, env, ctx.sshPrivateKey,
        [container, ...(consulDone ? [consulDone] : [])]);
}
```

The framework runs services in topological order based on `provides` /
`dependencies`, so `consul-setup` will always be in `ctx.commands` by the time
your `register` is called.

### Using Infisical secrets

`managedSecret` generates a random value on first run and stores it in
Infisical. Subsequent runs return the same value.

```typescript
import { managedSecret, lxcPassword } from "../../infisical";

export function register(ctx: ServiceContext): void {
    const myApiKey = managedSecret("my-service-api-key", ctx.infisicalConfig);
    // myApiKey is a pulumi.Output<string> — use it in environment: { ... }
}
```

Secrets appear in Infisical at the path configured in your Pulumi ESC
environment (default `/homelab`).

---

## Reverse proxy & Authentik

### YAML services
Set `reverseProxy` in `service.yaml` (see Option A above). The framework
handles the Consul service JSON automatically.

### TypeScript services
Write the Consul service JSON to `/etc/consul.d/<name>-service.json` in your
setup script, following the same pattern as `lxcs/hashistack/consul-setup.sh`:

```json
{
  "service": {
    "name": "my-service",
    "port": 8080,
    "tags": [
      "traefik.enable=true",
      "traefik.http.routers.my-service.rule=Host(`my-service.bagelindustries.com`)",
      "traefik.http.routers.my-service.entrypoints=web",
      "traefik.http.routers.my-service.middlewares=authentik@consulcatalog",
      "traefik.http.services.my-service.loadbalancer.server.port=8080"
    ]
  }
}
```

Remove the `middlewares` tag to make the service public.

Then reload Consul: `consul reload`.

### Authentik application

For Authentik to show the service in its app list (and for the forward-auth
flow to know which external URL to redirect back to after login), Authentik
needs a **Provider + Application** pair. Add it to the bootstrap script in
`lxcs/authentik/authentik-setup.sh`:

```python
protected = [
    ("My Service", "my-service", "http://my-service.bagelindustries.com"),
    # ...existing entries...
]
```

If Authentik is already deployed, you can add the pair through the Authentik
admin UI instead of re-running the bootstrap.

---

## Checklist

- [ ] Pick an unused VMID (201–229 for LXCs, 230–239 for VMs)
- [ ] Create `lxcs/<name>/service.yaml` **or** `lxcs/<name>/index.ts`
- [ ] Write `setup.sh` if the service needs any provisioning
- [ ] Add a `reverseProxy` block (YAML) or Consul JSON (script) if it needs a domain
- [ ] If Authentik-protected, add the app to the bootstrap list
- [ ] `pulumi preview` to verify the plan looks right before merging
