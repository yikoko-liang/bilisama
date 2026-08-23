// The microphone and the speaker, on the page where the echo canceller lives.
//
// Why here and not in Python: Chromium's canceller can only subtract audio the
// browser itself rendered. Capturing here while playback stayed in sounddevice
// would give it no reference signal and no effect, and the streamer would keep
// wearing headphones. So both ends live in this file.
//
// Two clocks, on purpose. Capture runs at whatever the device gave us and the
// worklet resamples down to 16k. Playback schedules 24k buffers against the
// context's own clock, because `nextStartTime` bookkeeping is the only way to
// get gapless streaming out of Web Audio — a source started "now" for each
// chunk leaves an audible seam every time the main thread is busy.

const DOWNLINK_RATE = 24000; // what the providers send (plan section 3.1)

export function createAudio({ onOwner }) {
  // Receipts go out on THIS socket, not the control one. They describe the
  // audio that just arrived here, and the control socket is a separate
  // connection opening on its own schedule — send a playback.started before it
  // is up and the frame is dropped, leaving the floor gate believing she is
  // silent while she talks. Measured: on a second page load the audio socket
  // consistently won the race and every started receipt vanished.
  const report = (event, data = {}) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ event, data }));
    }
  };

  let socket = null;
  let context = null;
  let capture = null; // MediaStreamAudioSourceNode + worklet, once granted
  let stream = null;
  let closed = false;
  let attempt = 0;
  let retryTimer = null;
  // When the next scheduled buffer should start, on the context clock. Behind
  // `currentTime` means we fell behind and the queue has drained.
  let nextStart = 0;
  let live = []; // scheduled sources, so a barge-in can stop them all
  let playedMs = 0; // of the current utterance, for playback.cancelled
  let analyser = null; // input level, for the panel's meter
  let wantedInput = undefined; // deviceId the streamer picked, if any

  const role = window.bilisamaShell ? "shell" : "browser";
  const url =
    `ws://${location.host}${location.pathname.replace(/\/$/, "")}` + `/audio?role=${role}`;

  const open = () => {
    if (closed) return;
    socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      attempt = 0;
      onOwner(role);
      startCapture();
    };
    socket.onmessage = (message) => schedule(new Int16Array(message.data));
    socket.onclose = (event) => {
      stopCapture();
      onOwner(null);
      if (closed) return;
      if (event.code === 4409) {
        // Another client holds the devices — the shell, almost always, with
        // this being a developer's tab. Retrying would be a hot loop against a
        // door that is not going to open.
        return;
      }
      const delay = Math.min(8000, 500 * 2 ** attempt) * (0.7 + Math.random() * 0.6);
      attempt += 1;
      retryTimer = setTimeout(open, delay);
    };
    socket.onerror = () => socket.close();
  };

  async function startCapture() {
    if (capture) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(wantedInput ? { deviceId: { exact: wantedInput } } : {}),
          // The whole reason this file exists. Noise suppression and gain
          // control ride along because the same capture chain runs them and
          // a streaming microphone wants both (plan section 3.2).
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (err) {
      // Denied, or no device. Say so once and stay silent: the reply audio
      // still plays, and the local sounddevice pair is not coming back while
      // this socket holds the claim.
      console.warn("拿不到麦克风：", err);
      onOwner(role, String(err));
      return;
    }
    context = context ?? new AudioContext();
    await context.audioWorklet.addModule(new URL("./capture-worklet.js", import.meta.url));
    const node = new AudioWorkletNode(context, "bilisama-capture");
    node.port.onmessage = (message) => {
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(message.data);
    };
    const source = context.createMediaStreamSource(stream);
    // Through a silent gain into the destination, which looks pointless and is
    // not: Web Audio renders by pulling backwards from the destination, so a
    // worklet with nothing downstream never has process() called at all. The
    // gain is zero because the alternative — microphone into speakers — is a
    // feedback loop.
    const mute = context.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(context.destination);
    // A parallel tap for the meter. fftSize is the smallest the spec allows:
    // this is a bar, not a spectrogram, and the cheapest read wins.
    analyser = context.createAnalyser();
    analyser.fftSize = 32;
    source.connect(analyser);
    capture = { node, source, mute };
  }

  function stopCapture() {
    capture?.source.disconnect();
    capture?.node.disconnect();
    capture?.mute.disconnect();
    capture?.node.port.close();
    capture = null;
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
  }

  function schedule(samples) {
    if (!samples.length) return;
    context = context ?? new AudioContext();
    // Autoplay policy parks a context until a gesture. In the shell there is
    // rarely one before she first speaks, so ask every time — resume() on a
    // running context is free.
    if (context.state === "suspended") context.resume().catch(() => {});
    if (!live.length && nextStart <= context.currentTime) {
      // The queue drained, so this buffer opens a new utterance. played_ms is
      // "how much of THIS reply was heard" — carrying it across would make the
      // number grow all session and mistrim the memory it feeds.
      playedMs = 0;
      nextStart = 0;
    }
    const buffer = context.createBuffer(1, samples.length, DOWNLINK_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1) channel[i] = samples[i] / 32768;

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    // A small cushion the first time: starting exactly at currentTime means
    // any scheduling jitter lands in the past and the browser drops the head
    // of the first word.
    const start = Math.max(nextStart, context.currentTime + 0.05);
    source.start(start);
    nextStart = start + buffer.duration;
    live.push(source);
    // Per SEGMENT, both of them. One reply plays as many buffers, and the
    // instant between two of them is not the end of speaking — reading it as
    // one is how the backlog comes back (ledger #41).
    report("playback.started");
    source.onended = () => {
      live = live.filter((other) => other !== source);
      playedMs += buffer.duration * 1000;
      report("playback.ended");
    };
  }

  open();

  return {
    /** Stop everything scheduled and report how much of it was heard. */
    clear() {
      const heard = Math.round(playedMs);
      live.forEach((source) => {
        source.onended = null; // no ended receipts for what nobody heard
        try {
          source.stop();
        } catch {
          // Already finished between the barge-in and this loop.
        }
      });
      live = [];
      nextStart = 0;
      playedMs = 0;
      report("playback.cancelled", { played_ms: heard });
    },
    /** 0..1 input loudness right now, or 0 when there is no microphone. */
    level() {
      if (!analyser) return 0;
      const bins = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(bins);
      let peak = 0;
      for (const bin of bins) peak = Math.max(peak, Math.abs(bin - 128) / 128);
      return peak;
    },
    /** Reopen capture on a different microphone. */
    async useInput(deviceId) {
      wantedInput = deviceId || undefined;
      stopCapture();
      await startCapture();
    },
    /** Move playback to a different speaker.
     *
     * Worth knowing before reaching for it: the echo canceller references the
     * output the browser considers current, so sending playback somewhere else
     * can quietly cost the cancellation this whole file exists for. The panel
     * says as much next to the control.
     */
    async useOutput(deviceId) {
      context = context ?? new AudioContext();
      if (typeof context.setSinkId !== "function") return false;
      await context.setSinkId(deviceId || "");
      return true;
    },
    /** A short tone down the real playback path, to confirm the right speaker. */
    async test() {
      context = context ?? new AudioContext();
      if (context.state === "suspended") await context.resume().catch(() => {});
      const osc = context.createOscillator();
      const gain = context.createGain();
      osc.frequency.value = 660;
      // Ramped, not switched: a square edge on a speaker is a click, and a
      // click is exactly what someone testing their audio should not hear.
      gain.gain.setValueAtTime(0, context.currentTime);
      gain.gain.linearRampToValueAtTime(0.18, context.currentTime + 0.02);
      gain.gain.linearRampToValueAtTime(0, context.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(context.destination);
      osc.start();
      osc.stop(context.currentTime + 0.36);
    },
    /** Devices the streamer can pick between, named. */
    async devices() {
      // Labels stay empty until permission is granted, so this is worth
      // nothing before capture starts — the panel calls it after.
      const all = await navigator.mediaDevices.enumerateDevices();
      return all
        .filter((device) => device.kind === "audioinput" || device.kind === "audiooutput")
        .map((device) => ({
          id: device.deviceId,
          kind: device.kind,
          label: device.label || "（未命名设备）",
        }));
    },
    close() {
      closed = true;
      clearTimeout(retryTimer);
      stopCapture();
      socket?.close();
    },
  };
}
