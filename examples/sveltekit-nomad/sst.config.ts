/// <reference path="./.sst/platform/config.d.ts" />
import * as command from "@pulumi/command";
import { execSync } from "child_process";
import * as path from "path";

const projectDir = process.cwd();

export default $config({
  app(input) {
    return {
      name: "sveltekit-nomad",
      removal: "remove",
      home: "local",
      providers: {
        command: "1.0.1",
      },
    };
  },

  async run() {
    // IPs match your homelab conventions: ip(vmid) = 192.168.0.<vmid>
    const NOMAD_SERVER = "192.168.0.202";  // submits jobs via nomad CLI
    const NOMAD_CLIENT = "192.168.0.230";  // runs Docker containers

    const sshKey = process.env.SSH_KEY_PATH ?? `${process.env.HOME}/.ssh/id_ed25519`;
    const gitHash = execSync("git rev-parse --short HEAD", { cwd: projectDir }).toString().trim();
    const image = `sveltekit-nomad:${$app.stage}-${gitHash}`;
    const jobFile = path.join(projectDir, "app.nomad.hcl");

    // Build the Docker image locally
    const build = new command.local.Command("build", {
      create: `docker build -t "${image}" "${projectDir}"`,
      triggers: [gitHash],
    });

    // Transfer image to the Nomad client — no registry needed
    const transfer = new command.local.Command("transfer", {
      create: `docker save "${image}" | ssh -i "${sshKey}" -o StrictHostKeyChecking=no root@${NOMAD_CLIENT} docker load`,
      triggers: [gitHash],
    }, { dependsOn: [build] });

    // Submit the Nomad job, passing the image tag as a variable
    const deploy = new command.local.Command("deploy", {
      create: `ssh -i "${sshKey}" -o StrictHostKeyChecking=no root@${NOMAD_SERVER} "nomad job run -var 'image=${image}' -" < "${jobFile}"`,
      triggers: [gitHash],
    }, { dependsOn: [transfer] });

    return {
      image,
      nomadOutput: deploy.stdout,
    };
  },
});
