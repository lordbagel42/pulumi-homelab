import * as path from "path";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { nodeLxcTemplate } from "../../framework/node-assets";
import { ip, ServiceContext } from "../../framework";
import { ansibleProvision } from "../../utils/ansible";

export const name = "amp-runner";
export const provides = ["amp-runner-setup"];

export function register(ctx: ServiceContext): void {
  const address = ip(214);
  const machine = new ProxmoxMachine(
    name,
    {
      type: "lxc",
      nodeName: "tower",
      templateFileId: nodeLxcTemplate("tower", ctx.provider),
      vmId: 214,
      ip: address,
      // Tower: 4 physical cores, ~15 GiB usable RAM. This allocation assumes
      // sandbox-next (232) has been removed, not merely stopped.
      cpu: 3,
      cpuLimit: 3,
      cpuUnits: 25,
      memory: 12288,
      swap: 0,
      disk: 96,
      nesting: true,
      // Match the existing LXC; changing its privilege mode forces replacement.
      privileged: true,
      tags: ["ai", "development"],
      sshKeys: [ctx.sshKey],
      password: ctx.vmPassword,
    },
    { provider: ctx.provider, protect: true },
  );

  const provision = ansibleProvision("amp-runner-provision", {
    host: address,
    playbookPath: path.join(__dirname, "playbook.yml"),
    sshPrivateKey: ctx.sshPrivateKey,
    dependsOn: [machine],
  });
  ctx.commands.set("amp-runner-setup", provision);
}
