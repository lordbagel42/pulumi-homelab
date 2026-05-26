import * as crypto from "crypto";
import * as fs from "fs";
import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";

export function sshSetup(
    name: string,
    host: string,
    scriptPath: string,
    env: Record<string, string>,
    sshPrivateKey: pulumi.Output<string>,
    dependsOn: pulumi.Resource[],
): command.local.Command {
    const remoteEnv = Object.entries(env)
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");

    const scriptHash = crypto.createHash("sha256").update(fs.readFileSync(scriptPath)).digest("hex");

    return new command.local.Command(name, {
        create: `
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "$_SSH_KEY" > "$key"
echo "Waiting for SSH on ${host}..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${host} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done
ssh -i "$key" -o StrictHostKeyChecking=no root@${host} "sed -i '/^#\\?UseDNS/d' /etc/ssh/sshd_config && echo 'UseDNS no' >> /etc/ssh/sshd_config && systemctl restart ssh || true"
ssh -i "$key" -o StrictHostKeyChecking=no root@${host} "${remoteEnv} bash -s" < "${scriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        triggers: [scriptHash],
        environment: {
            _SSH_KEY: sshPrivateKey,
        },
    }, { dependsOn });
}
