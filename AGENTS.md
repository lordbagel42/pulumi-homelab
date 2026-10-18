# pulumi-homelab

## Secrets live in Infisical, never in Pulumi

All secrets for this stack must be stored in Infisical (project
`923d970a-8e38-48f5-8c38-e7197c1ce36c`, environment `prod`). This is not optional.

- Never store a secret in Pulumi stack config. No `secure:` values in
  `Pulumi.*.yaml`, and never run `pulumi config set --secret`. Plain, non-secret
  settings (flags, IDs, public SSH host keys) may stay in stack config.
- Read secrets in code with the helpers in `infisical/index.ts`:
  `readSecret` for values that must already exist, `optionalSecret` for values a
  service can run without, and `managedSecret`/`lxcPassword` for values the stack
  may generate. Put each service's secrets in its own folder (for example
  `/huddle-phone`), named `SERVICE_PURPOSE` in upper snake case.
- The only secrets that reach Pulumi without these helpers are the bootstrap
  values in the Pulumi ESC environment `default/homelab`, and that environment
  reads them from Infisical itself. Add new bootstrap values to Infisical first
  and reference them from ESC; never put a literal secret in ESC.
- When you find a secret stored anywhere else, move it into Infisical, switch the
  code to read it from there, and delete the old copy.
- Never print, log or commit secret values while doing any of this.
