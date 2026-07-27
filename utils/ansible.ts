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
    /** Extra variables to pass to Ansible */
    extraVars?: Record<string, any>;
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

    // extraVars are JSON-encoded before any Output resolves, so an Output does
    // not serialise to its value — it serialises to Pulumi's "Calling [toJSON]
    // on an [Output<T>] is not supported" message. That lands in whatever file
    // the playbook renders and corrupts it silently, so reject it up front.
    for (const [key, value] of Object.entries(args.extraVars ?? {})) {
        if (pulumi.Output.isInstance(value)) {
            throw new Error(
                `ansibleProvision("${name}"): extraVars.${key} is a pulumi Output, which cannot survive ` +
                `JSON encoding. Pass it over SSH out-of-band instead — see lxcs/authentik/index.ts.`,
            );
        }
    }

    const extraVarsJson = args.extraVars ? JSON.stringify(args.extraVars) : "{}";

    return new command.local.Command(name, {
        create: pulumi.interpolate`
key=$(mktemp)
chmod 600 "$key"
printf '%s\n' "${args.sshPrivateKey}" > "$key"

inventory=$(mktemp)
echo "${args.host} ansible_user=root ansible_ssh_private_key_file=$key ansible_ssh_extra_args='-o StrictHostKeyChecking=no'" > "$inventory"

echo "Waiting for SSH on ${args.host}..."
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${args.host} echo ok 2>/dev/null; then
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done

ansible-playbook -i "$inventory" "${args.playbookPath}" --extra-vars '${extraVarsJson}'
rc=\$?

rm -f "$key" "$inventory"
if [ \$rc -ne 0 ]; then
  return \$rc
fi
        `,
        triggers: [playbookHash, extraVarsJson],
    }, { dependsOn: args.dependsOn });
}
