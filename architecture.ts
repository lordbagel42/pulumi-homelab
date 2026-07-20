// Network: 192.168.0.0/24
//   .1         gateway
//   .2–10      physical hardware (manually assigned)
//   .11–200    DHCP pool
//   .100       proxmox-backup-server (static, vmId 100)
//   .201–210   system lxc   — vmId matches last octet  (consul: 201, nomad: 202, traefik: 203, cloudflared: 204)
//   .211–229   app lxc      — vmId matches last octet  (dokploy: 211)
//   .230–239   VMs (static) — vmId matches last octet  (nomad-client: 230, sandbox: 231)
//   .240–255   reserved for shenanigans like sst.dev
//
// Rule: for any static resource, vmId = last IP octet.
//   staticIp(201) → 192.168.0.201
//
// ID Ranges:
//   0–99:    Proxmox templates
//   100–199: DHCP / ephemeral VMs   (proxmox-backup-server: 100)
//   200–210: system lxc             (consul-server: 201, nomad-server: 202, traefik: 203, cloudflared: 204)
//   211–229: app lxc                (dokploy: 211)
//   230–239: static VMs             (nomad-client: 230, sandbox: 231)

import { LXCDefinition } from "./lxc-config";
import { testingVms } from "./vms/testing";

export const NETWORK_GATEWAY = "192.168.0.1";

/** Static /24 address for any resource where vmId = last octet (range 201–254). */
export const staticIp = (vmId: number): string => `192.168.0.${vmId}`;

export const allLxcs: LXCDefinition[] = [];
export const allVms = [...testingVms];
