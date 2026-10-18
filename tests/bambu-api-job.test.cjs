const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const { test } = require("node:test");
const ts = require("typescript");
const pulumi = require("@pulumi/pulumi");
const nomad = require("@pulumi/nomad");

require.extensions[".ts"] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    module._compile(outputText, filename);
};

test("bambu-api writes the Infisical data key as BAMBU_DATA_KEY and submits the job in JSON mode", async () => {
    const resources = new Map();
    const calls = [];
    const dataKey = "ab".repeat(32);
    await pulumi.runtime.setMocks({
        newResource(args) {
            resources.set(args.name, args.inputs);
            const state = args.name === "infisical-secret-BAMBU_API_DATA_KEY" ? { ...args.inputs, stdout: dataKey } : args.inputs;
            return { id: `${args.name}-id`, state };
        },
        call(args) {
            calls.push(args);
            return { ...args.inputs, id: "parsed-job", json: '{"ID":"bambu-api","Stop":false}' };
        },
    }, "pulumi-homelab", "test");
    pulumi.runtime.setAllConfig({
        "bambu-api:enabled": "true",
        "bambu-api:sshHostKey": "test-host-key",
        "bambu-api:image": "bambu-api:abc1234",
    });

    const { register } = require("../nomad/bambu-api/index.ts");
    const provider = new nomad.Provider("test-nomad", { address: "http://localhost:4646" });
    const infisicalConfig = { clientId: "id", clientSecret: "secret", projectId: "project", environment: "prod", secretPath: "/" };
    const ctx = { nomadProvider: provider, infisicalConfig, sshPrivateKey: pulumi.secret("test-key"), commands: new Map() };
    register(ctx);
    const job = ctx.commands.get("bambu-api-deploy");
    await job.id.promise();

    const env = resources.get("bambu-api-environment");
    // The dotenv file reaches the host only as a secret.
    assert.deepEqual(env.stdin, { "4dabf18193072939515e22adb298388d": "1b47061264138c4ac30d75fd1eb44270", value: `BAMBU_DATA_KEY=${dataKey}\n` });
    assert.equal(env.logging, "none");
    assert.equal(resources.get("bambu-api").json, true);
    assert.equal(resources.get("bambu-api").purgeOnDestroy, false);
    assert.deepEqual(JSON.parse(calls[0].inputs.variables), {
        image: "bambu-api:abc1234",
        environment_revision: crypto.createHash("sha256").update(`BAMBU_DATA_KEY=${dataKey}\n`).digest("hex"),
    });
    assert.equal(await pulumi.isSecret(job.jobspec), true);
});
