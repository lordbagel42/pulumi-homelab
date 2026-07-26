import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as fs from "fs";
import * as crypto from "crypto";

export interface AnsibleProvisionArgs {
    /** Target IP address */
    host: string;
    /** Path to the playbook file */
    playbookPath: string;
    /** Extra variables to pass to Ansible. Values may be Outputs (e.g. secrets). */
    extraVars?: Record<string, any>;
    /** SSH private key for authentication */
    sshPrivateKey: pulumi.Output<string>;
    /** Resources that must be ready before provisioning starts */
    dependsOn?: pulumi.Input<pulumi.Resource> | pulumi.Input<pulumi.Resource>[];
}

/**
 * Trigger fingerprint for extraVars.
 *
 * Output values cannot be serialised at graph-construction time, so they are
 * represented by their key name only. Plain values are included verbatim so a
 * changed literal still forces a re-run.
 */
function extraVarsFingerprint(extraVars: Record<string, any>): string {
    const stable: Record<string, unknown> = {};
    for (const key of Object.keys(extraVars).sort()) {
        const value = extraVars[key];
        stable[key] = pulumi.Output.isInstance(value) ? "<output>" : value;
    }
    return JSON.stringify(stable);
}

export function ansibleProvision(
    name: string,
    args: AnsibleProvisionArgs,
): command.local.Command {
    const playbookHash = crypto.createHash("sha256").update(fs.readFileSync(args.playbookPath)).digest("hex");

    // pulumi.output() unwraps nested Outputs, so secrets resolve to real values
    // at apply time instead of stringifying to Pulumi's toJSON error message.
    const extraVarsJson = args.extraVars
        ? pulumi.output(args.extraVars).apply((vars) => JSON.stringify(vars))
        : pulumi.output("{}");

    return new command.local.Command(name, {
        interpreter: ["/bin/bash", "-c"],
        // Secrets are passed through the environment (and a 0600 temp file) so
        // they never land in the resource's create script or in process argv.
        environment: {
            _SSH_PRIVATE_KEY: args.sshPrivateKey,
            _EXTRA_VARS: extraVarsJson,
        },
        create: `
set -uo pipefail

key=$(mktemp)
vars=$(mktemp)
inventory=$(mktemp)
chmod 600 "$key" "$vars"
trap 'rm -f "$key" "$vars" "$inventory"' EXIT

printf '%s\\n' "$_SSH_PRIVATE_KEY" > "$key"
printf '%s' "$_EXTRA_VARS" > "$vars"

echo "${args.host} ansible_user=root ansible_ssh_private_key_file=$key ansible_ssh_extra_args='-o StrictHostKeyChecking=no'" > "$inventory"

echo "Waiting for SSH on ${args.host}..."
ssh_ready=false
for i in $(seq 1 40); do
  if ssh -i "$key" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes root@${args.host} echo ok 2>/dev/null; then
    ssh_ready=true
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done

if [ "$ssh_ready" != "true" ]; then
  echo "ERROR: SSH on ${args.host} never came up after 200s" >&2
  exit 1
fi

ansible-playbook -i "$inventory" "${args.playbookPath}" --extra-vars "@$vars"
        `.trim(),
        triggers: [playbookHash, extraVarsFingerprint(args.extraVars ?? {})],
    }, { dependsOn: args.dependsOn });
}
