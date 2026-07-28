import * as pulumi from "@pulumi/pulumi";
import * as proxmox from "@muhlba91/pulumi-proxmoxve";

/**
 * The cloud-init user-data every generic Debian VM in the homelab boots with.
 *
 * Shared so that per-node copies of the snippet are byte-identical: a VM that
 * moves between nodes must not also pick up a different cloud-init, and PVE
 * looks snippets up on the VM's own node.
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

export interface NodeVmAssetsArgs {
    provider: proxmox.Provider;
    sshKey: string;
    vmPassword: pulumi.Input<string>;
}

export interface NodeVmAssets {
    /** Pass to a VM disk's `importFrom`. */
    debianCloudImageId: pulumi.Output<string>;
    /** Pass to a VM's `initialization.userDataFileId`. */
    cloudInitSnippetId: pulumi.Output<string>;
}

/**
 * Cloud image + cloud-init snippet on a specific node's `local` datastore.
 *
 * `local` is node-local storage, not shared across the cluster, so a VM can only
 * import a disk from — or read a snippet out of — the copy that lives on its own
 * node. The context-wide `debianCloudImageId`/`cloudInitSnippetId` are optiplex's;
 * a VM pinned to any other node needs its own pair, or PVE fails the create with
 * `failed to stat '/var/lib/vz/import/debian-12-genericcloud-amd64.qcow2'`.
 */
export function nodeVmAssets(
    nodeName: string,
    { provider, sshKey, vmPassword }: NodeVmAssetsArgs,
): NodeVmAssets {
    const image = new proxmox.download.File(`${nodeName}-debian-cloud-image`, {
        contentType: "import",
        datastoreId: "local",
        nodeName,
        url: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        fileName: "debian-12-genericcloud-amd64.qcow2",
        // See the matching note in index.ts — "latest" moves, and the size check
        // would otherwise force a replacement PVE refuses while the old file is
        // still on disk.
        overwrite: false,
        overwriteUnmanaged: true,
    }, { provider });

    const snippet = new proxmox.FileLegacy(`${nodeName}-vm-cloud-init`, {
        contentType: "snippets",
        datastoreId: "local",
        nodeName,
        sourceRaw: {
            fileName: "vm-cloud-init.yaml",
            data: vmCloudInitData(sshKey, vmPassword),
        },
    }, { provider });

    return {
        debianCloudImageId: image.id,
        cloudInitSnippetId: snippet.id,
    };
}
