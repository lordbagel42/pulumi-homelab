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
ssh -i "$key" -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=60 -o ServerAliveCountMax=10 root@${host} "${remoteEnv} bash -s" < "${scriptPath}"
rc=$?
rm -f "$key"
exit $rc
        `.trim(),
        environment: {
            _SSH_KEY: sshPrivateKey,
        },
    }, { dependsOn });
}
