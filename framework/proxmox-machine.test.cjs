const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const ts = require("typescript");
const pulumi = require("@pulumi/pulumi");

// Compile local TypeScript without adding a second test/tooling dependency.
require.extensions[".ts"] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
  });
  module._compile(outputText, filename);
};

pulumi.runtime.setMocks({
  newResource: ({ name, inputs }) => ({ id: name, state: inputs }),
  call: ({ inputs }) => inputs,
});
const { ProxmoxMachine } = require("./proxmox-machine.ts");
const base = {
  type: "lxc",
  vmId: 214,
  ip: "192.168.0.214",
  sshKeys: [],
  password: "test-only",
};

test("LXC resource receives an independent CPU quota, low weight, and explicit zero swap", async () => {
  const machine = new ProxmoxMachine("limited", {
    ...base,
    cpu: 3,
    cpuLimit: 2.5,
    cpuUnits: 25,
    memory: 12288,
    swap: 0,
  });
  const cpu = await machine.machine.cpu.promise();
  const memory = await machine.machine.memory.promise();
  assert.deepEqual(cpu, { cores: 3, limit: 2.5, units: 25 });
  assert.deepEqual(memory, { dedicated: 12288, swap: 0 });
});

test("existing LXCs retain their provider defaults when limits are omitted", async () => {
  const machine = new ProxmoxMachine("existing", base);
  const cpu = await machine.machine.cpu.promise();
  const memory = await machine.machine.memory.promise();
  assert.deepEqual(cpu, { cores: 1 });
  assert.deepEqual(memory, { dedicated: 512 });
});
