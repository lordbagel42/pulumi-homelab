import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

/** The LXC template every container in the homelab is built from. */
export const LXC_TEMPLATE_FILE = "debian-13-standard_13.1-2_amd64.tar.zst";

/**
 * The cloud-init user-data every generic Debian VM in the homelab boots with.
 *
 * Shared so that per-node copies of the snippet stay byte-identical: PVE looks
 * snippets up on the VM's own node, so a VM that moves nodes needs a copy there
 * and must not pick up a different cloud-init along the way.
 */
export function vmCloudInitData(
    sshKey: string,
    vmPassword: pulumi.Input<string>,
): pulumi.Output<string> {
    return pulumi.interpolate`#cloud-config
users:
  - name: sylvie
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    ssh_authorized_keys:
      - ${sshKey}
  - name: root
    ssh_authorized_keys:
      - ${sshKey}
chpasswd:
  users:
    - name: sylvie
      password: ${vmPassword}
      type: text
  expire: false
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
  - sed -i '/PermitRootLogin/d' /etc/ssh/sshd_config
  - echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
  - sed -i '/^#\?UseDNS/d' /etc/ssh/sshd_config
  - echo 'UseDNS no' >> /etc/ssh/sshd_config
  - systemctl restart sshd
`;
}

/**
 * The LXC template used by every container in the homelab, on a specific node's
 * `local` datastore.
 *
 * `local` is node-local storage, not shared across the cluster, so a container
 * can only be created from the template that sits on its own node. optiplex's
 * copy is placed by hand (`local:vztmpl/...`); any other node gets a managed
 * download instead of a missing-file failure at create time.
 */
export function nodeLxcTemplate(
    nodeName: string,
    provider: proxmox.Provider,
): pulumi.Output<string> {
    const template = new proxmox.download.File(`${nodeName}-lxc-template`, {
        contentType: "vztmpl",
        datastoreId: "local",
        nodeName,
        url: `http://download.proxmox.com/images/system/${LXC_TEMPLATE_FILE}`,
        fileName: LXC_TEMPLATE_FILE,
        overwrite: false,
        overwriteUnmanaged: true,
    }, { provider });

    return template.id;
}
