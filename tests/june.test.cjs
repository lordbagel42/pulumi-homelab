const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { test } = require("node:test");
const ts = require("typescript");
const pulumi = require("@pulumi/pulumi");
const { getMonitor } = require("@pulumi/pulumi/runtime/settings");

require.extensions[".ts"] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    module._compile(outputText, filename);
};

const hostKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPuU5lQjHmJzJqWTYEFy2hUKUctFIBCCzFQpM8m66MVa june@test";
const operatorToken = "test_operator_token_0123456789abcdef";

function context() {
    return {
        sshKey: "ssh-ed25519 test-provisioning-key",
        sshPrivateKey: pulumi.secret("test-private-key"),
        commands: new Map(),
    };
}

function configure(values, secrets = []) {
    pulumi.runtime.setAllConfig(
        Object.fromEntries(Object.entries(values).map(([key, value]) => [`june:${key}`, String(value)])),
        secrets.map((key) => `june:${key}`),
    );
}

function unwrapSecret(value) {
    return value && typeof value === "object" && Object.hasOwn(value, "value") ? value.value : value;
}

async function waitFor(predicate) {
    const deadline = Date.now() + 2_000;
    while (!predicate()) {
        if (Date.now() >= deadline) throw new Error("timed out waiting for Pulumi mock registration");
        await new Promise((resolve) => setTimeout(resolve, 5));
    }
}

function python(arguments) {
    return spawnSync("python3", arguments, {
        encoding: "utf8",
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
}

test("June gates enrollment and provisions only a protected private LXC from a secret payload", async () => {
    const resources = [];
    const registrations = [];
    await pulumi.runtime.setMocks({
        newResource(args) {
            resources.push(args);
            return { id: `${args.name}-id`, state: args.inputs };
        },
        call(args) {
            if (args.token === "cloudflare:index/getZone:getZone") {
                return { ...args.inputs, id: "test-zone-id", account: { id: "test-account" } };
            }
            return args.inputs;
        },
    }, "pulumi-homelab", "test");
    const monitor = getMonitor();
    const registerResource = monitor.registerResource.bind(monitor);
    monitor.registerResource = (request, callback) => {
        registrations.push({
            name: request.getName(),
            type: request.getType(),
            protect: request.getProtect(),
            additionalSecretOutputs: request.getAdditionalsecretoutputsList(),
        });
        return registerResource(request, callback);
    };

    const { register } = require("../lxcs/june/index.ts");

    configure({ enabled: false });
    const disabled = context();
    const disabledStart = resources.length;
    register(disabled);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(disabled.commands.size, 0, "disabled June must register nothing");
    assert.equal(resources.length, disabledStart, "disabled June must not create a machine");

    configure({ enabled: true, rootPassword: "test-root-password" }, ["rootPassword"]);
    const unenrolled = context();
    const unenrolledStart = resources.length;
    register(unenrolled);
    assert.equal(unenrolled.commands.size, 0, "a machine without a verified host key must not be provisioned");
    await waitFor(() => resources.slice(unenrolledStart).some((resource) =>
        resource.name === "june" && resource.type === "proxmoxve:index/containerLegacy:ContainerLegacy"));
    assert.equal(resources.slice(unenrolledStart).some((resource) => resource.type.startsWith("command:remote:")), false);

    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-pulumi-test-"));
    const archive = path.join(temporary, "june-source.tar.gz");
    fs.writeFileSync(archive, "first public source snapshot");
    configure({
        enabled: true,
        rootPassword: "test-root-password",
        sshHostKey: hostKey,
        operatorToken,
        sourceArchive: archive,
    }, ["rootPassword", "operatorToken"]);
    const enrolled = context();
    register(enrolled);
    const setup = enrolled.commands.get("june-setup");
    assert.ok(setup, "verified enrollment must register provisioning");
    await setup.id.promise();

    const latest = (name) => resources.findLast((resource) => resource.name === name);
    const container = latest("june");
    assert.equal(container.type, "proxmoxve:index/containerLegacy:ContainerLegacy");
    assert.equal(container.inputs.nodeName, "optiplex");
    assert.equal(container.inputs.vmId, 215);
    assert.deepEqual(container.inputs.cpu, { cores: 2 });
    assert.deepEqual(container.inputs.memory, { dedicated: 2048 });
    assert.deepEqual(container.inputs.disk, { datastoreId: "local-lvm", size: 12 });
    assert.equal(container.inputs.unprivileged, true);
    const initialization = unwrapSecret(container.inputs.initialization);
    assert.notEqual(initialization, container.inputs.initialization, "the root password must taint initialization as secret");
    assert.equal(initialization.ipConfigs[0].ipv4.address, "192.168.0.215/16");

    for (const type of [
        "homelab:framework:ProxmoxMachine",
        "proxmoxve:index/containerLegacy:ContainerLegacy",
    ]) {
        const registration = registrations.findLast((item) => item.name === "june" && item.type === type);
        assert.equal(registration?.protect, true, `${type} must be protected`);
    }

    for (const name of ["june-stage", "june-source", "june-provision"]) {
        assert.equal(registrations.findLast((item) => item.name === name)?.protect, false,
            `${name} must be replaceable without removing protection from the LXC`);
        assert.equal(latest(name).inputs.delete, undefined, "artifact bookkeeping must not delete remote state");
    }

    const provision = latest("june-provision");
    assert.equal(provision.inputs.logging, "none");
    assert.equal(provision.inputs.addPreviousOutputInEnv, false);
    const connection = unwrapSecret(provision.inputs.connection);
    assert.equal(connection.host, "192.168.0.215");
    assert.equal(connection.hostKey, hostKey);
    assert.equal(await pulumi.isSecret(setup.connection), true);
    assert.equal(await pulumi.isSecret(setup.stdin), true);
    assert.equal(provision.inputs.create.includes(operatorToken), false);
    assert.equal(provision.inputs.update.includes(operatorToken), false);
    const provisionRegistration = registrations.findLast((item) => item.name === "june-provision");
    assert.equal(provisionRegistration.additionalSecretOutputs.includes("stdout"), true);
    assert.equal(provisionRegistration.additionalSecretOutputs.includes("stderr"), true);

    const payload = JSON.parse(await setup.stdin.promise());
    assert.equal(payload.operatorToken, operatorToken);
    assert.equal(payload.revision, crypto.createHash("sha256").update("first public source snapshot").digest("hex"));
    assert.deepEqual(JSON.parse(payload.config), {
        host: "192.168.0.215",
        port: 3080,
        setupMode: true,
        owner: { id: "raygen", identities: [] },
        model: {
            protocol: "codex",
            model: "gpt-6-astra",
            home: "/var/lib/june/.codex",
            executable: "/opt/june/current/node_modules/.bin/codex",
        },
        coding: { enabled: false },
        console: { origin: "http://127.0.0.1:3080" },
    });

    const juneTypes = resources.map((resource) => resource.type);
    assert.equal(juneTypes.some((type) => type.startsWith("cloudflare:")), false);
    assert.equal(juneTypes.some((type) => type.startsWith("consul:")), false);

    const firstTriggers = await setup.triggers.promise();
    fs.writeFileSync(archive, "second public source snapshot");
    const updated = context();
    register(updated);
    const updatedSetup = updated.commands.get("june-setup");
    await updatedSetup.id.promise();
    const secondTriggers = await updatedSetup.triggers.promise();
    assert.notEqual(secondTriggers[0], firstTriggers[0], "source bytes must trigger a new immutable release");
    assert.equal(latest("june-source").inputs.triggers[0], secondTriggers[0]);

    configure({
        enabled: true,
        rootPassword: "test-root-password",
        sshHostKey: hostKey,
        operatorToken,
        sourceArchive: archive,
        slack: JSON.stringify({ teamId: "TWORKSPACE", botUserId: "UBOT", ownerUserId: "UOWNER" }),
        slackBotToken: "xoxb-test-bot-token-not-a-real-credential",
        slackSigningSecret: "0123456789abcdef0123456789abcdef",
    }, ["rootPassword", "operatorToken", "slackBotToken", "slackSigningSecret"]);
    assert.throws(() => register(context()), /Slack ingress requires/, "Slack must not silently start without its ingress providers");
    const slack = context();
    slack.consulProvider = new (require("@pulumi/consul").Provider)("june-test-consul", { address: "127.0.0.1:8500" });
    slack.cloudflareProvider = new (require("@pulumi/cloudflare").Provider)("june-test-cloudflare", { apiToken: pulumi.secret("test-token") });
    slack.cloudflaredTunnelToken = pulumi.secret(Buffer.from(JSON.stringify({ t: "test-tunnel-id" })).toString("base64"));
    register(slack);
    const slackSetup = slack.commands.get("june-setup");
    await slackSetup.id.promise();
    await waitFor(() => latest("june-slack-service") && latest("june-slack-dns"));
    const slackPayload = JSON.parse(await slackSetup.stdin.promise());
    const runtime = JSON.parse(slackPayload.config);
    assert.equal(runtime.setupMode, false);
    assert.deepEqual(runtime.owner.identities, [{ channel: "slack", accountId: "TWORKSPACE", senderId: "UOWNER" }]);
    assert.deepEqual(runtime.slack, {
        teamId: "TWORKSPACE", botUserId: "UBOT",
        signingSecretEnv: "SLACK_SIGNING_SECRET", botTokenEnv: "SLACK_BOT_TOKEN",
    });
    assert.equal(slackPayload.slack.botToken, "xoxb-test-bot-token-not-a-real-credential");
    assert.equal(slackPayload.slack.signingSecret, "0123456789abcdef0123456789abcdef");
    assert.equal(await pulumi.isSecret(slackSetup.stdin), true);
    assert.equal(slackPayload.config.includes(slackPayload.slack.botToken), false);
    assert.equal(latest("june-provision").inputs.create.includes(slackPayload.slack.botToken), false);
    const service = latest("june-slack-service").inputs;
    assert.equal(service.address, "192.168.0.215");
    assert.equal(service.port, 3080);
    assert.deepEqual(service.tags.filter((tag) => tag.includes(".rule=")), [
        "traefik.http.routers.june-slack.rule=Host(`june-slack.bagelindustries.com`) && Path(`/webhooks/slack`) && Method(`POST`)",
    ], "no host-only fallback may expose the operator API");
    assert.equal(latest("june-slack-dns").inputs.name, "june-slack.bagelindustries.com");
    assert.equal(latest("june-slack-dns").inputs.proxied, true);

    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's installer accepts only the documented public source archive layout", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-installer-test-"));
    const source = path.join(temporary, "source");
    fs.mkdirSync(path.join(source, "src"), { recursive: true });
    fs.writeFileSync(path.join(source, "package.json"), JSON.stringify({ packageManager: "pnpm@10.33.0" }));
    fs.writeFileSync(path.join(source, "pnpm-lock.yaml"), "lockfileVersion: '9.0'\n");
    fs.writeFileSync(path.join(source, "pnpm-workspace.yaml"), "packages: []\n");
    fs.writeFileSync(path.join(source, "tsconfig.json"), "{}\n");
    fs.writeFileSync(path.join(source, "src/main.ts"), "export {};\n");
    const valid = path.join(temporary, "valid.tar.gz");
    assert.equal(spawnSync("tar", ["-czf", valid, "-C", source, "."]).status, 0);

    const provisioner = path.join(__dirname, "../lxcs/june/provision.py");
    const accepted = python([provisioner, "--validate-source-archive", valid]);
    assert.equal(accepted.status, 0, accepted.stderr);

    fs.writeFileSync(path.join(source, ".env"), "JUNE_OPERATOR_TOKEN=must-not-ship\n");
    const privateArchive = path.join(temporary, "private.tar.gz");
    assert.equal(spawnSync("tar", ["-czf", privateArchive, "-C", source, "."]).status, 0);
    const rejected = python([provisioner, "--validate-source-archive", privateArchive]);
    assert.notEqual(rejected.status, 0);
    assert.match(rejected.stderr, /allowlisted public source/i);

    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's immutable release validation accepts root-owned package symlinks", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-release-test-"));
    const target = path.join(temporary, "tsx-cli.mjs");
    const link = path.join(temporary, "tsx");
    fs.writeFileSync(target, "export {};\n", { mode: 0o644 });
    fs.symlinkSync("tsx-cli.mjs", link);
    const provisioner = path.join(__dirname, "../lxcs/june/provision.py");
    const probe = `
import importlib.util
import os
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("june_provision", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
metadata = Path(sys.argv[2]).lstat()
if not module.is_immutable_entry(metadata, os.getuid(), os.getgid()):
    raise SystemExit("package-manager symlink was treated as writable")
`;
    const result = python(["-c", probe, provisioner, link]);
    assert.equal(result.status, 0, result.stderr);
    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's activation restores the previous service when staging configuration fails", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-rollback-test-"));
    const provisioner = path.join(__dirname, "../lxcs/june/provision.py");
    const probe = `
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

spec = importlib.util.spec_from_file_location("june_provision", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = Path(sys.argv[2])
module.CONFIG = root / "etc/june/config.json"
module.CREDENTIALS = root / "etc/june/credentials"
module.SERVICE = root / "etc/systemd/system/june.service"
module.LOGIN_HELPER = root / "usr/local/sbin/june-start-login"
old_release = root / "opt/june/releases" / ("a" * 64)
new_release = root / "opt/june/releases" / ("b" * 64)
old_release.mkdir(parents=True)
new_release.mkdir(parents=True)
old = {
    module.CONFIG: b"old config\\n",
    module.CREDENTIALS: b"JUNE_OPERATOR_TOKEN=old_token_value_0123456789\\n",
    module.SERVICE: b"old service\\n",
    module.LOGIN_HELPER: b"old login helper\\n",
}
for path, content in old.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600 if path == module.CREDENTIALS else 0o644)
active = {"target": old_release}
module.current_target = lambda: active["target"]
module.switch_current = lambda target: active.update(target=target)
module.service_state = lambda action: True
module.wait_for_health = lambda timeout=120: True
module.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
real_write = module.write
failed = {"once": False}
def fail_service_once(path, content, mode=0o644, owner=None):
    if Path(path) == module.SERVICE and not failed["once"]:
        failed["once"] = True
        raise OSError("simulated service staging failure")
    return real_write(path, content, mode, owner)
module.write = fail_service_once
payload = {
    "config": "new config\\n",
    "operatorToken": "new_operator_token_0123456789abcdef",
    "service": "new service\\n",
    "startLogin": "new login helper\\n",
}
try:
    module.activate(new_release, payload)
except RuntimeError:
    pass
else:
    raise SystemExit("activation unexpectedly succeeded")
if active["target"] != old_release:
    raise SystemExit("previous release link was not restored")
for path, content in old.items():
    if path.read_bytes() != content:
        raise SystemExit(f"previous file was not restored: {path}")
`;
    const result = python(["-c", probe, provisioner, temporary]);
    assert.equal(result.status, 0, result.stderr);
    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's root provisioner refuses service-user symlinks in private state", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-state-test-"));
    const target = path.join(temporary, "target");
    const link = path.join(temporary, "rivet");
    fs.mkdirSync(target);
    fs.symlinkSync(target, link);
    const provisioner = path.join(__dirname, "../lxcs/june/provision.py");
    const probe = `
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys
spec = importlib.util.spec_from_file_location("june_provision", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
try:
    module.ensure_private_directory(Path(sys.argv[2]), account)
except ValueError:
    pass
else:
    raise SystemExit("private-state symlink was followed")
`;
    const result = python(["-c", probe, provisioner, link]);
    assert.equal(result.status, 0, result.stderr);
    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's public release parents remain traversable under the private installer umask", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-public-dir-test-"));
    const directory = path.join(temporary, "opt/june/releases");
    const provisioner = path.join(__dirname, "../lxcs/june/provision.py");
    const probe = `
import importlib.util
import os
from pathlib import Path
import stat
import sys
spec = importlib.util.spec_from_file_location("june_provision", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
os.umask(0o077)
directory = Path(sys.argv[2])
module.ensure_public_directory(directory)
if stat.S_IMODE(directory.stat().st_mode) != 0o755:
    raise SystemExit("public release parent is not traversable")
`;
    const result = python(["-c", probe, provisioner, directory]);
    assert.equal(result.status, 0, result.stderr);
    fs.rmSync(temporary, { recursive: true, force: true });
});

test("June's service launches the app without interpreting pnpm shell shims as JavaScript", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-entrypoint-test-"));
    try {
        const modules = path.join(temporary, "node_modules");
        fs.mkdirSync(path.join(modules, "tsx"), { recursive: true });
        fs.mkdirSync(path.join(modules, ".bin"));
        fs.mkdirSync(path.join(temporary, "src"));
        fs.writeFileSync(path.join(modules, "tsx/package.json"), JSON.stringify({
            type: "module", exports: "./loader.mjs",
        }));
        fs.writeFileSync(path.join(modules, "tsx/loader.mjs"), "export {};\n");
        fs.writeFileSync(path.join(modules, ".bin/tsx"), '#!/bin/sh\nexec node "$@"\n', { mode: 0o755 });
        fs.writeFileSync(path.join(temporary, "src/main.js"), 'console.log("entrypoint-ok");\n');
        const unit = fs.readFileSync(path.join(__dirname, "../lxcs/june/june.service"), "utf8");
        const args = unit.match(/^ExecStart=(.*)$/m)[1].split(" ").map((arg) =>
            arg.replace("/opt/june/current/", "").replace("src/main.ts", "src/main.js"));
        args[0] = process.execPath;
        const result = spawnSync(args[0], args.slice(1), { cwd: temporary, encoding: "utf8" });
        assert.equal(result.status, 0, result.stderr);
        assert.equal(result.stdout.trim(), "entrypoint-ok");
    } finally {
        fs.rmSync(temporary, { recursive: true, force: true });
    }
});

test("June activates Slack with private credentials and rejects identity or environment injection", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-slack-test-"));
    const probe = `
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import sys

spec = importlib.util.spec_from_file_location("june_provision", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = Path(sys.argv[2])
module.JUNE_ROOT = root / "opt/june"
source = Path(sys.argv[1]).with_name("source.tar.gz")
revision = hashlib.sha256(source.read_bytes()).hexdigest()
archive = module.JUNE_ROOT / "incoming" / f"{revision}.tar.gz"
archive.parent.mkdir(parents=True)
shutil.copyfile(source, archive)
runtime = {
    "host": "192.168.0.215", "port": 3080, "setupMode": False,
    "owner": {"id": "raygen", "identities": [{"channel": "slack", "accountId": "TWORKSPACE", "senderId": "UOWNER"}]},
    "model": {"protocol": "codex", "model": "gpt-6-astra", "home": "/var/lib/june/.codex", "executable": "/opt/june/current/node_modules/.bin/codex"},
    "coding": {"enabled": False},
    "console": {"origin": "http://127.0.0.1:3080"},
    "slack": {"teamId": "TWORKSPACE", "botUserId": "UBOT", "signingSecretEnv": "SLACK_SIGNING_SECRET", "botTokenEnv": "SLACK_BOT_TOKEN"},
}
payload = {
    "archive": str(archive), "revision": revision,
    "operatorToken": "operator_token_0123456789abcdef0123456789",
    "config": json.dumps(runtime), "service": "unit", "startLogin": "helper",
    "slack": {"teamId": "TWORKSPACE", "botUserId": "UBOT", "ownerUserId": "UOWNER", "botToken": "xoxb-test-bot-token-not-a-real-credential", "signingSecret": "0123456789abcdef0123456789abcdef"},
}
module.validate_payload(payload)
for key, value in [("botToken", "xoxb-test\\nJUNE_ALLOW_NATIVE_CODING=1"), ("teamId", "EORG"), ("ownerUserId", "UBOT"), ("signingSecret", "not-a-signing-secret")]:
    bad = copy.deepcopy(payload)
    bad["slack"][key] = value
    try:
        module.validate_payload(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"invalid Slack {key} accepted")
bad = copy.deepcopy(payload)
bad_runtime = copy.deepcopy(runtime)
bad_runtime["owner"]["identities"][0]["senderId"] = "USOMEONEELSE"
bad["config"] = json.dumps(bad_runtime)
try:
    module.validate_payload(bad)
except ValueError:
    pass
else:
    raise AssertionError("mismatched owner accepted")
for console in (None, {"origin": "https://june-slack.bagelindustries.com"}, {"origin": "http://localhost:3080"}):
    bad = copy.deepcopy(payload)
    bad_runtime = copy.deepcopy(runtime)
    if console is None:
        del bad_runtime["console"]
    else:
        bad_runtime["console"] = console
    bad["config"] = json.dumps(bad_runtime)
    try:
        module.validate_payload(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("missing or changed private console origin accepted")
module.CONFIG = root / "etc/june/config.json"
module.CREDENTIALS = root / "etc/june/credentials"
module.SERVICE = root / "etc/systemd/system/june.service"
module.LOGIN_HELPER = root / "usr/local/sbin/june-start-login"
module.current_target = lambda: None
module.switch_current = lambda target: None
module.service_state = lambda action: False
module.wait_for_health = lambda timeout=120: True
module.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
module.activate(root / "release", payload)
assert module.CREDENTIALS.read_text() == "JUNE_OPERATOR_TOKEN=operator_token_0123456789abcdef0123456789\\nSLACK_BOT_TOKEN=xoxb-test-bot-token-not-a-real-credential\\nSLACK_SIGNING_SECRET=0123456789abcdef0123456789abcdef\\n"
assert stat.S_IMODE(module.CREDENTIALS.stat().st_mode) == 0o600
assert "xoxb-" not in module.CONFIG.read_text()
`;
    try {
        const result = python(["-c", probe, path.join(__dirname, "../lxcs/june/provision.py"), temporary]);
        assert.equal(result.status, 0, result.stderr);
    } finally {
        fs.rmSync(temporary, { recursive: true, force: true });
    }
});

test("June's scoped helper includes only its own ingress in the deployment targets", () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "june-targets-test-"));
    try {
        fs.writeFileSync(path.join(temporary, "pulumi"), '#!/bin/sh\nprintf "%s\\n" "$@"\n', { mode: 0o755 });
        const script = path.join(__dirname, "../lxcs/june/pulumi-service.sh");
        const prefix = "urn:pulumi:homelab::pulumi-homelab";
        const ingressTargets = [
            `${prefix}::consul:index/node:Node::june-slack-node`,
            `${prefix}::consul:index/service:Service::june-slack-service`,
            `${prefix}::cloudflare:index/dnsRecord:DnsRecord::june-slack-dns`,
        ];
        for (const phase of ["ingress", "all"]) {
            const result = spawnSync("bash", [script, phase, "preview", "--non-interactive"], {
                encoding: "utf8", env: { ...process.env, PATH: `${temporary}:${process.env.PATH}` },
            });
            assert.equal(result.status, 0, result.stderr);
            const args = result.stdout.trim().split("\n");
            assert.deepEqual(args.slice(0, 3), ["preview", "--stack", "homelab"]);
            const targets = args.filter((_, index) => args[index - 1] === "--target");
            assert.deepEqual(targets.slice(-3), ingressTargets);
            assert.equal(targets.length, phase === "ingress" ? 3 : 8);
            assert.equal(args.includes("--target-dependents"), false);
        }
    } finally {
        fs.rmSync(temporary, { recursive: true, force: true });
    }
});
