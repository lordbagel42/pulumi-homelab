import * as fs from "fs";
import * as path from "path";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import { ProxmoxMachine } from "../../framework/proxmox-machine";
import { ip, ServiceContext } from "../../framework";

export const name = "chatgpt-bridge";
export const provides = ["chatgpt-bridge-setup"];

export function register(ctx: ServiceContext): void {
    const config = new pulumi.Config(name);
    if (!config.getBoolean("enabled")) return;

    const address = ip(213);
    const machine = new ProxmoxMachine(name, {
        type: "lxc",
        vmId: 213,
        ip: address,
        cpu: 1,
        memory: 1024,
        disk: 8,
        // Debian 13 systemd needs mount namespaces for credentials and services.
        nesting: true,
        tags: ["home-assistant", "chatgpt"],
        sshKeys: [ctx.sshKey],
        password: config.requireSecret("rootPassword"),
    }, { provider: ctx.provider, protect: true });

    // First create the container, then enroll its SSH host key through the
    // Proxmox console before sending the bridge credential over SSH.
    const hostKey = config.get("sshHostKey");
    if (!hostKey) return;

    const install = `set -eu
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates </dev/null
fi
python3 -c 'import json,sys; payload=json.load(sys.stdin); namespace={"__name__":"provisioner"}; exec(compile(payload.pop("provisioner"), "provision.py", "exec"), namespace); namespace["main"](payload)'
`;
    const files = {
        provisioner: fs.readFileSync(path.join(__dirname, "provision.py"), "utf8"),
        gateway: fs.readFileSync(path.join(__dirname, "chatgpt_gateway.py"), "utf8"),
        service: fs.readFileSync(path.join(__dirname, "home-assistant-chatgpt.service"), "utf8"),
    };
    const payload = config.requireSecret("token").apply((token) => JSON.stringify({
        token,
        ...files,
    }));
    const provision = new command.remote.Command("chatgpt-bridge-provision", {
        connection: {
            host: address,
            user: "root",
            privateKey: ctx.sshPrivateKey,
            hostKey,
            dialErrorLimit: 20,
            perDialTimeout: 10,
        },
        create: install,
        update: install,
        stdin: payload,
        logging: "none",
        addPreviousOutputInEnv: false,
    }, {
        parent: machine,
        dependsOn: [machine.machine],
        additionalSecretOutputs: ["stdout", "stderr"],
        customTimeouts: { create: "10m", update: "10m" },
    });
    ctx.commands.set("chatgpt-bridge-setup", provision);
}
