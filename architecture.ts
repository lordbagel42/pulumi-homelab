// Network: 192.168.0.0/24 worth of addresses, but configured as a /16.
//
// The addressing plan below is a /24 and stays one — every static IP here is
// 192.168.0.x. The NETMASK is /16 (framework.LAN_PREFIX), because this segment
// is one flat L2 shared with mrow's Kubernetes nodes on 192.168.10.x and its
// Cilium service addresses on 192.168.11-12.x. A /24 netmask sends replies to
// those through the Eero, which has no route for them and drops them.
//   .1         gateway
//   .2–10      physical hardware (manually assigned)
//   .11–200    DHCP pool
//   .200       home-assistant (not managed here; needs a DHCP reservation)
//   .100       proxmox-backup-server (static, vmId 100)
//   .201–210   system lxc   — vmId matches last octet  (consul: 201, nomad: 202, traefik: 203, cloudflared: 204)
//   .211–229   app lxc      — vmId matches last octet  (dokploy: 211, garage: 212,
//                                                       authentik: 220)
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
//   211–229: app lxc                (dokploy: 211, garage: 212, authentik: 220)
//   230–239: static VMs             (nomad-client: 230, sandbox: 231)

import { LXCDefinition } from "./lxc-config";
import { testingVms } from "./vms/testing";

export const NETWORK_GATEWAY = "192.168.0.1";

/**
 * Static address for any resource where vmId = last octet (range 201–254).
 *
 * The plan is /24-shaped; the interface is configured /16. See the header and
 * framework.LAN_PREFIX for why those differ.
 */
export const staticIp = (vmId: number): string => `192.168.0.${vmId}`;

export const allLxcs: LXCDefinition[] = [];
export const allVms = [...testingVms];
