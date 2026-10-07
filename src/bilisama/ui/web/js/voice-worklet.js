// Her voice's last stop before the speaker: a peak limiter behind the volume
// knob (audio.voice_volume, the GainNode in audio.js).
//
// Why a worklet: Chromium's DynamicsCompressorNode is not a limiter. Measured
// on a real duplex recording doubled (2026-10-08, chromium 151): it still let
// samples past full scale (peak 1.075), and below a 0 dB threshold it adds
// makeup gain that changes her level at 100%.
//
// It never has to clip. Output is delayed by one render quantum (128 frames,
// under 3 ms at 48 kHz), and that held-back block is the lookahead: each
// block's gain ramps from where the previous one ended to a value safe for
// both the block going out and the one that just came in, so every block
// starts at a gain its own loudest sample can take. With nothing near the
// ceiling the gain stays exactly 1 and the samples go through untouched.
//
// dev_talk.py's _VoiceVolume applies the same rule on the local speaker;
// tests/unit/test_voice_volume.py holds these two constants to its copies.

const CEILING = 0.98;
const RELEASE_S = 0.1;
// Within this of the gain it is heading for, a release lands exactly, so it
// ends on 1.0 and the samples go through untouched again.
const SNAP = 1e-4;

const peakOf = (block) => {
  let peak = 0;
  for (let i = 0; i < block.length; i += 1) peak = Math.max(peak, Math.abs(block[i]));
  return peak;
};

class VoiceLimiter extends AudioWorkletProcessor {
  constructor() {
    super();
    this.held = null; // the block going out next, one quantum late
    this.silence = null; // stands in for an input with nothing connected
    this.gain = 1; // where the last block's ramp ended
  }

  process(inputs, outputs) {
    const output = outputs[0][0];
    const frames = output.length;
    if (!this.held || this.held.length !== frames) {
      this.held = new Float32Array(frames);
      this.silence = new Float32Array(frames);
    }
    const incoming = inputs[0]?.[0] ?? this.silence;
    const own = peakOf(this.held);
    const loudest = Math.max(own, peakOf(incoming));
    const safeNow = own > CEILING ? CEILING / own : 1;
    const safe = loudest > CEILING ? CEILING / loudest : 1;
    const start = Math.min(this.gain, safeNow);
    let end = safe;
    if (end > start) {
      // A boost coming back after a loud word: slowly, or it pumps.
      end = start + (end - start) * (1 - Math.exp(-frames / (RELEASE_S * sampleRate)));
      if (safe - end < SNAP) end = safe;
    }
    this.gain = end;
    if (start === 1 && end === 1) {
      output.set(this.held);
    } else {
      const step = (end - start) / frames;
      for (let i = 0; i < frames; i += 1) output[i] = this.held[i] * (start + step * (i + 1));
    }
    // Copied, not kept: the engine reuses its input arrays between calls.
    this.held.set(incoming);
    return true; // the playback path; it lives as long as the page does
  }
}

registerProcessor("bilisama-voice-limiter", VoiceLimiter);
