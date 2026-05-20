import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";

export interface InfisicalConfig {
    clientId: pulumi.Input<string>;
    clientSecret: pulumi.Input<string>;
    projectId: pulumi.Input<string>;
    environment: string;
    secretPath: string;
    host?: string;
}

export function managedSecret(secretName: string, config: InfisicalConfig): pulumi.Output<string> {
    return _getOrCreateSecret(secretName, config, `openssl rand -hex 32`, `infisical-secret-${secretName}`);
}

/** Reads an existing secret; fails hard if not found rather than generating a placeholder. */
export function readSecret(secretName: string, config: InfisicalConfig): pulumi.Output<string> {
    return _getOrCreateSecret(
        secretName,
        config,
        `printf 'readSecret: "%s" not found at "%s" (HTTP %s): %s\\n' '${secretName}' '${config.secretPath}' "$STATUS" "$_INFISICAL_BODY" >&2; exit 1`,
        `read-secret-${secretName}`,
    );
}

export function lxcPassword(containerName: string, config: InfisicalConfig): pulumi.Output<string> {
    return _getOrCreateSecret(
        `lxc-root-${containerName}`,
        config,
        `openssl rand -base64 24 | tr '+/' '-_' | tr -d '='`,
        `lxc-password-${containerName}`,
    );
}

function _getOrCreateSecret(secretName: string, config: InfisicalConfig, generateCmd: string, resourceName: string): pulumi.Output<string> {
    const host = config.host ?? "https://app.infisical.com";
    const encodedPath = encodeURIComponent(config.secretPath);

    const pathParts = config.secretPath.replace(/^\//, "").split("/").filter(Boolean);
    const folderName = pathParts[pathParts.length - 1] ?? "";
    const folderParent = pathParts.length > 1 ? "/" + pathParts.slice(0, -1).join("/") : "/";

    // No delete script: secret persists in Infisical across pulumi destroy cycles.
    const cmd = new command.local.Command(resourceName, {
        interpreter: ["/bin/bash", "-c"],
        create: `
set -euo pipefail

AUTH_RESP=$(curl -s -X POST "${host}/api/v1/auth/universal-auth/login" \\
    -H "Content-Type: application/json" \\
    -d "$(jq -n --arg c "$_CLIENT_ID" --arg s "$_CLIENT_SECRET" '{clientId:$c,clientSecret:$s}')")
ACCESS_TOKEN=$(printf '%s' "$AUTH_RESP" | jq -r '.accessToken // empty')
if [ -z "$ACCESS_TOKEN" ]; then
    printf 'Infisical auth failed: %s\\n' "$AUTH_RESP" >&2
    exit 1
fi

curl -s -X POST "${host}/api/v1/folders" \\
    -H "Authorization: Bearer $ACCESS_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d "$(jq -n \\
        --arg p "$_PROJECT_ID" \\
        --arg e "${config.environment}" \\
        --arg path "${folderParent}" \\
        --arg name "${folderName}" \\
        '{workspaceId:$p,environment:$e,path:$path,name:$name}')" \\
    > /dev/null 2>&1 || true

RESP=$(mktemp)
STATUS=$(curl -s -o "$RESP" -w "%{http_code}" \\
    -H "Authorization: Bearer $ACCESS_TOKEN" \\
    "${host}/api/v3/secrets/raw/${secretName}?workspaceId=$_PROJECT_ID&environment=${config.environment}&secretPath=${encodedPath}&type=shared")

if [ "$STATUS" = "200" ]; then
    jq -r '.secret.secretValue' "$RESP"
    rm -f "$RESP"
    exit 0
fi
_INFISICAL_BODY=$(cat "$RESP" 2>/dev/null | head -c 800)
rm -f "$RESP"

VALUE=$(${generateCmd})

CREATE_RESP=$(curl -s -w "\\n%{http_code}" -X POST "${host}/api/v3/secrets/raw/${secretName}" \\
    -H "Authorization: Bearer $ACCESS_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d "$(jq -n \\
        --arg p "$_PROJECT_ID" \\
        --arg e "${config.environment}" \\
        --arg sp "${config.secretPath}" \\
        --arg v "$VALUE" \\
        '{workspaceId:$p,environment:$e,secretPath:$sp,secretValue:$v,type:"shared"}')")
CREATE_STATUS=$(printf '%s' "$CREATE_RESP" | tail -1)
if [ "$CREATE_STATUS" != "200" ]; then
    printf 'Infisical create failed (HTTP %s): %s\\n' "$CREATE_STATUS" "$(printf '%s' "$CREATE_RESP" | head -1)" >&2
    exit 1
fi

printf '%s' "$VALUE"
        `.trim(),
        environment: {
            _CLIENT_ID: config.clientId,
            _CLIENT_SECRET: config.clientSecret,
            _PROJECT_ID: config.projectId,
        },
    });

    return pulumi.secret(cmd.stdout);
}
