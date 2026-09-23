const assert = require("node:assert/strict");
const fs = require("node:fs");
const { test } = require("node:test");
const ts = require("typescript");
const pulumi = require("@pulumi/pulumi");
const nomad = require("@pulumi/nomad");

// Use the source directory so register() reads its real HCL and shell assets.
require.extensions[".ts"] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    module._compile(outputText, filename);
};

test("ai-proxy submits parser JSON in JSON mode, retaining typed variables and secret propagation", async () => {
    const resources = new Map();
    const calls = [];
    // Nomad API JSON includes Stop, which is not a valid HCL job argument.
    const parsed = '{"ID":"ai-proxy","Name":"ai-proxy","Stop":false}';
    await pulumi.runtime.setMocks({
        newResource(args) {
            resources.set(args.name, args.inputs);
            return { id: `${args.name}-id`, state: args.inputs };
        },
        call(args) {
            calls.push(args);
            assert.equal(args.token, "nomad:index/getJobParser:getJobParser");
            return { ...args.inputs, id: "parsed-job", json: parsed };
        },
    }, "pulumi-homelab", "test");

    const { register } = require("../nomad/ai-proxy/index.ts");
    const provider = new nomad.Provider("test-nomad", { address: "http://localhost:4646" });
    for (const [replicas, servingEnabled] of [[0, false], [1, true]]) {
        calls.length = 0;
        pulumi.runtime.setAllConfig({
            "ai-proxy:enabled": "true",
            "ai-proxy:sshHostKey": "test-host-key",
            "ai-proxy:image": "registry.example/ai-proxy:revision-123",
            "ai-proxy:environment": "abc",
            "ai-proxy:replicas": String(replicas),
            "ai-proxy:servingEnabled": String(servingEnabled),
        }, ["ai-proxy:environment"]);
        const ctx = { nomadProvider: provider, sshPrivateKey: pulumi.secret("test-key"), commands: new Map() };
        register(ctx);
        const job = ctx.commands.get("ai-proxy-deploy");
        await job.id.promise();

        assert.equal(resources.get("ai-proxy").json, true);
        assert.equal(await job.jobspec.promise(), parsed);
        assert.equal(resources.get("ai-proxy").hcl2, undefined);
        assert.equal(calls.length, 1);
        assert.deepEqual(JSON.parse(calls[0].inputs.variables), {
            image: "registry.example/ai-proxy:revision-123",
            replicas,
            serving_enabled: servingEnabled,
            environment_revision: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        });
        assert.equal(await pulumi.isSecret(job.jobspec), true);
        assert.equal(resources.get("ai-proxy").detach, false);
        assert.equal(resources.get("ai-proxy").purgeOnDestroy, false);
    }
});
