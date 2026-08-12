import * as fs from "fs";
import * as path from "path";
import * as nomad from "@pulumi/nomad";
import { NOMAD_IP } from "../../lxcs/hashistack";
import type { ServiceContext } from "../../framework";

export const name = "mcp-server";
export const provides = ["mcp-server-deploy"];
export const dependencies = ["nomad-setup"];

export function register(ctx: ServiceContext): void {
    // ── Nomad provider (homelab cluster) ───────────────────────────────────────
    const nomadProvider = new nomad.Provider("mcp-nomad", {
        address: `http://${NOMAD_IP}:4646`,
    });

    const jobPath = path.join(__dirname, "mcp.nomad.hcl");

    new nomad.Job("mcp-server", {
        jobspec: fs.readFileSync(jobPath, "utf-8"),
    }, { provider: nomadProvider });
}
