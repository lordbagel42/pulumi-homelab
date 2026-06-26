import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as path from "path";
import * as fs from "fs";
import * as crypto from "crypto";

export interface AnsibleProvisionArgs {
    /** Target IP address */
    host: string;
    /** Path to the playbook file */
    playbookPath: string;
    /** Extra variables to pass to Ansible (supports Pulumi Outputs) */
    extraVars?: Record<string, pulumi.Input<any>>;
    /** SSH private key for authentication */
    sshPrivateKey: pulumi.Output<string>;
    /** Resources that must be ready before provisioning starts */
    dependsOn?: pulumi.Input<pulumi.Resource> | pulumi.Input<pulumi.Resource>[];
}

export function ansibleProvision(
    name: string,
    args: AnsibleProvisionArgs,
): command.local.Command {
    const playbookHash = crypto.createHash("sha256").update(fs.readFileSync(args.playbookPath)).digest("hex");

    const extraVarsJson: pulumi.Output<string> = args.extraVars
        ? pulumi.all(args.extraVars).apply(resolved => JSON.stringify(resolved))
        : pulumi.output("{}");

    return new command.local.Command(name, {
        create: pulumi.interpolate`
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "${args.sshPrivateKey}" > "$key"

inventory=$(mktemp)
echo "${args.host} ansible_user=root ansible_ssh_private_key_file=$key ansible_ssh_extra_args='-o StrictHostKeyChecking=no'" > "$inventory"

extra_vars=$(mktemp)
printf '%s\n' '${extraVarsJson}' > "$extra_vars"

echo "Waiting for SSH on ${args.host}..."
for i in \$(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${args.host} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt \$i/40..."
  sleep 5
done

ansible-playbook -i "$inventory" "${args.playbookPath}" --extra-vars "@$extra_vars"
rc=\$?

rm -f "$key" "$inventory" "$extra_vars"
exit \$rc
        `,
        triggers: [playbookHash, extraVarsJson],
    }, { dependsOn: args.dependsOn });
}
