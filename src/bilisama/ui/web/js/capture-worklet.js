class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    let cursor = 0;
    while (cursor < input.length) {
      const count = Math.min(input.length - cursor, this.buffer.length - this.offset);
      this.buffer.set(input.subarray(cursor, cursor + count), this.offset);
      this.offset += count;
      cursor += count;
      if (this.offset === this.buffer.length) {
        this.port.postMessage(this.buffer, [this.buffer.buffer]);
        this.buffer = new Float32Array(2048);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("bilisama-capture", CaptureProcessor);
