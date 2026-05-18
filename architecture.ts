// Network: 192.168.0.0/24
//   .1         gateway
//   .2–10      physical hardware (manually assigned)
//   .11–200    DHCP pool
//   .201–210   system lxc   — vmId matches last octet  (hashistack: 201–202)
//   .211–229   app lxc      — vmId matches last octet
//   .230–239   VMs (static) — vmId matches last octet
//   .240–255   reserved for shenanigans like sst.dev
//
// Rule: for any static resource, vmId = last IP octet.
//   staticIp(201) → 192.168.0.201
//
// ID Ranges:
//   0–99:    Proxmox templates
//   100–199: DHCP / ephemeral VMs   (ubuntu-testing: 100)
//   200–210: system lxc             (consul-server: 201, nomad-server: 202)
//   211–229: app lxc
//   230–239: static VMs

import { LXCDefinition } from "./lxc-config";
import { testingVms } from "./vms/testing";

export const NETWORK_GATEWAY = "192.168.0.1";

/** Static /24 address for any resource where vmId = last octet (range 201–254). */
export const staticIp = (vmId: number): string => `192.168.0.${vmId}`;

export const allLxcs: LXCDefinition[] = [];
export const allVms = [...testingVms];
