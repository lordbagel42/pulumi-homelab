// ID Ranges:
//   0–199:   system / templates
//     190–199: hashistack lxc    (consul-server: 190, nomad-server: 191)
//   200–399: LXC containers
//   400–599: VMs
//     400–409: testing           (ubuntu-testing: 400)

import { hashistackLxcs } from "./lxcs/hashistack";
import { testingVms } from "./vms/testing";

export const allLxcs = [...hashistackLxcs];
export const allVms = [...testingVms];
