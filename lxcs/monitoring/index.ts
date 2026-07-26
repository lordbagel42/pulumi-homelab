import type { ServiceContext } from "../../framework";

// Grafana Alloy is installed natively on every host by the respective modules:
//   - consul, nomad-server, traefik, nomad-client → hashistack/index.ts
//   - cloudflared → cloudflared/index.ts
//   - authentik → authentik/index.ts
// This module is a placeholder; add any future cross-cutting monitoring here.

export const name = "monitoring";
export const provides: string[] = [];
export const dependencies: string[] = [];

export function register(_ctx: ServiceContext): void {}
