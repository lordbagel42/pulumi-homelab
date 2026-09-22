// Both nodes run in an 8 kHz AudioContext. Chromium resamples the Chime stream
// before it reaches this processor; no naive sample dropping or aliasing.
class PhoneInput extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Float32Array(4000); // Never accumulate >500 ms latency.
    this.read = 0;
    this.size = 0;
    this.port.onmessage = ({ data }) => {
      const view = new DataView(data);
      for (let i = 0; i + 1 < data.byteLength; i += 2) {
        if (this.size === this.samples.length) {
          this.read = (this.read + 1) % this.samples.length;
          this.size--;
        }
        this.samples[(this.read + this.size) % this.samples.length] = view.getInt16(i, true) / 32768;
        this.size++;
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0][0];
    for (let i = 0; i < output.length; i++) {
      output[i] = this.size ? this.samples[this.read] : 0;
      if (this.size) {
        this.read = (this.read + 1) % this.samples.length;
        this.size--;
      }
    }
    return true;
  }
}

class PhoneOutput extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new ArrayBuffer(320); // 20 ms of signed 16-bit little endian PCM.
    this.view = new DataView(this.frame);
    this.position = 0;
  }

  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const sample = Math.max(-1, Math.min(1, input[i]));
      this.view.setInt16(this.position * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
      if (++this.position === 160) {
        this.port.postMessage(this.frame, [this.frame]);
        this.frame = new ArrayBuffer(320);
        this.view = new DataView(this.frame);
        this.position = 0;
      }
    }
    return true;
  }
}

registerProcessor("phone-input", PhoneInput);
registerProcessor("phone-output", PhoneOutput);
