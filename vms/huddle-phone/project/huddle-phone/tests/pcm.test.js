import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

function processors() {
  const result = {};
  const sandbox = {
    AudioWorkletProcessor: class {
      constructor() { this.port = { messages: [], postMessage(data) { this.messages.push(data); } }; }
    },
    registerProcessor(name, processor) { result[name] = processor; },
    Float32Array, ArrayBuffer, DataView,
  };
  vm.runInNewContext(readFileSync(new URL("../web/pcm-worklet.js", import.meta.url), "utf8"), sandbox);
  return result;
}

test("phone input decodes signed little-endian PCM and emits silence on underrun", () => {
  const classes = processors();
  const input = new classes["phone-input"]();
  const frame = new ArrayBuffer(8);
  const view = new DataView(frame);
  [-32768, -16384, 0, 16384].forEach((v, i) => view.setInt16(i * 2, v, true));
  input.port.onmessage({ data: frame });
  const output = new Float32Array(8);
  input.process([], [[output]]);
  assert.deepEqual(Array.from(output), [-1, -0.5, 0, 0.5, 0, 0, 0, 0]);
});

test("input congestion keeps the most recent 500 ms rather than growing latency", () => {
  const classes = processors();
  const input = new classes["phone-input"]();
  const frame = new ArrayBuffer(10000);
  const view = new DataView(frame);
  for (let i = 0; i < 5000; i++) view.setInt16(i * 2, i, true);
  input.port.onmessage({ data: frame });
  assert.equal(input.size, 4000);
  const output = new Float32Array(1);
  input.process([], [[output]]);
  assert.equal(output[0], 1000 / 32768);
});

test("Chime output forms complete 20 ms packets across worklet render boundaries", () => {
  const classes = processors();
  const output = new classes["phone-output"]();
  for (let i = 0; i < 5; i++) output.process([[new Float32Array(128).fill(0.5)]]);
  assert.equal(output.port.messages.length, 4);
  for (const frame of output.port.messages) {
    assert.equal(frame.byteLength, 320);
    for (let i = 0; i < 160; i++) assert.equal(new DataView(frame).getInt16(i * 2, true), 16384);
  }
});

test("PCM output saturates safely at both endpoints", () => {
  const classes = processors();
  const output = new classes["phone-output"]();
  const input = new Float32Array(160).fill(2);
  input[0] = -2;
  output.process([[input]]);
  const view = new DataView(output.port.messages[0]);
  assert.equal(view.getInt16(0, true), -32768);
  assert.equal(view.getInt16(2, true), 32767);
});
