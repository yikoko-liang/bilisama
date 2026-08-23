// Microphone capture, resampled to the 16 kHz the providers take.
//
// A worklet rather than ScriptProcessor: this runs on the audio thread, so a
// busy main thread — a skin mounting, a long panel render — cannot stretch or
// drop frames. The plan costed the old ScriptProcessor at about 25ms of
// avoidable latency (section 2.8's frontend list).
//
// The resampling is not optional. `getUserMedia` takes a sampleRate hint and
// browsers routinely ignore it: the context runs at whatever the device gave,
// usually 48000, and shipping those samples as if they were 16000 makes the
// streamer sound three times too slow to the server. Linear interpolation is
// enough going down from 48k — the input is already band-limited by the
// browser's own capture chain, which ran the echo canceller and noise
// suppression before we ever see it.

const TARGET_RATE = 16000;
// 20ms at 16 kHz. Section 3.2 asks for 20ms frames; smaller ones cost a
// message per frame for no latency the socket can actually deliver.
const FRAME = 320;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    // Fractional read position into the incoming block, carried across blocks
    // so the phase never jumps: resetting it every 128 samples would add a
    // periodic click at the block rate.
    this.cursor = 0;
    this.pending = new Int16Array(FRAME);
    this.filled = 0;
    // The last sample of the previous block, so interpolation at the seam has
    // a left neighbour instead of silence.
    this.tail = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true; // no input yet; keep the node alive

    while (this.cursor < channel.length) {
      const index = Math.floor(this.cursor);
      const frac = this.cursor - index;
      const left = index === 0 ? this.tail : channel[index - 1];
      const right = channel[index];
      const value = left + (right - left) * frac;
      // Clamp before scaling: a value past ±1 wraps to the opposite sign in
      // Int16Array, turning a loud moment into a burst of noise.
      const clamped = Math.max(-1, Math.min(1, value));
      this.pending[this.filled] = Math.round(clamped * 32767);
      this.filled += 1;
      if (this.filled === FRAME) {
        // Transfer the buffer rather than copy it — this runs 50 times a
        // second for the whole stream.
        const frame = this.pending;
        this.pending = new Int16Array(FRAME);
        this.filled = 0;
        this.port.postMessage(frame.buffer, [frame.buffer]);
      }
      this.cursor += this.ratio;
    }
    this.cursor -= channel.length;
    this.tail = channel[channel.length - 1];
    return true;
  }
}

registerProcessor("bilisama-capture", CaptureProcessor);
