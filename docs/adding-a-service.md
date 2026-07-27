# Adding a Service (New Framework)

Services live under `lxcs/` (containers) or `vms/` (full VMs). The framework
auto-discovers every subdirectory that contains either a `service.yaml` or an
`index.ts` with a `register` export.

## ProxmoxMachine Component

The new framework uses the `ProxmoxMachine` component for unified management of LXCs and VMs.

### Features
- **Unified API**: Same component for LXC and VM.
- **Auto-Registration**: Built-in support for Consul service registration and Traefik tags.
- **Ansible Integration**: Seamlessly provision using Ansible playbooks.

## Example: TypeScript Service with Ansible

**1. Create a directory**
`lxcs/my-service/index.ts`
`lxcs/my-service/playbook.yml`

**2. Write `index.ts`**
```typescript
import * as path from "path";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ansibleProvision } from "../../utils/ansible";
import { lxcPassword } from "../../infisical";
import { ip } from "../../framework";
import type { ServiceContext } from "../../framework";

export const name = "my-service";
export const provides = ["my-service-setup"];

export function register(ctx: ServiceContext) {
    const VMID = 251;
    const IP = ip(VMID);

    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: VMID,
        ip: IP,
        sshKeys: [ctx.sshKey],
        password: lxcPassword(name, ctx.infisicalConfig),
        reverseProxy: {
            domain: "my-service.bagelindustries.com",
            port: 8080,
            protected: true,
        },
        // Pass consulProvider if you want auto-registration in Consul
    }, { provider: ctx.provider });

    const provision = ansibleProvision("my-service-provision", {
        host: IP,
        playbookPath: path.join(__dirname, "playbook.yml"),
        sshPrivateKey: ctx.sshPrivateKey,
        extraVars: {
            my_var: "hello",
        },
        dependsOn: [machine],
    });

    ctx.commands.set("my-service-setup", provision);
}
```

**3. Write `playbook.yml`**
```yaml
- name: Provision My Service
  hosts: all
  become: yes
  tasks:
    - name: Install Nginx
      apt:
        name: nginx
        state: present
        update_cache: yes
```

## Exposing it to the internet

A `reverseProxy` block registers the service in Consul with Traefik tags, and
Traefik is already behind a Cloudflare tunnel — so the route works externally as
soon as the hostname points at the tunnel. See [external-routing.md](./external-routing.md)
for that step and for how the homelab and Oracle edges divide the catalog.

## IP / VMID convention
Every static resource follows the rule: **IP last-octet = VMID**.
Pick an unused ID (201–254) and use `ip(ID)` to get the address.

## Advantages of Ansible
- **Idempotency**: Running the same playbook multiple times won't break things.
- **Declarative**: Describe the state you want, not the steps to get there.
- **Rich Module Ecosystem**: Easy to manage users, packages, docker, etc.
