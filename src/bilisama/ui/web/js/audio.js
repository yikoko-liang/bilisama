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
// How fast her volume follows the slider: 95% of the way in 90 ms. A jump
// would click; slower than this and a drag no longer feels attached.
const VOLUME_TAU_S = 0.03;

/** Her voice's own volume, then a peak limiter, then the speaker.
 *
 * Every buffer she plays goes into `input`. The knob is a GainNode, so a
 * change lands on audio already scheduled (she arrives faster than real time,
 * so most of a reply is queued before the streamer reaches for the slider)
 * and never touches the wire. Above 100% the limiter in voice-worklet.js is
 * what keeps the louder sound from clipping, so a boost waits for it: until
 * the module has loaded, or if it never does, the knob goes no higher than
 * 100%. Quieter needs no guard and always works.
 *
 * The limiter goes in only when `quiet()` says nothing is scheduled. It delays
 * the sound by one render quantum, and swapping it into a sentence would make
 * the sound jump by that much — a click. `attach()` is how the caller says
 * the moment has come.
 *
 * Args (second argument):
 *   volume: Initial gain, 1 = as she arrived.
 *   quiet: True while nothing is playing through `input`.
 *   onTrouble: Called once with the error if the limiter cannot load.
 */
export function createVoiceChain(
  context,
  { volume = 1, quiet = () => true, onTrouble = () => {} } = {},
) {
  const knob = context.createGain();
  let wanted = volume;
  let loaded = null; // the limiter, built and waiting for a quiet moment
  let limiter = null; // the limiter, in the path
  const allowed = () => (limiter ? wanted : Math.min(wanted, 1));
  knob.gain.value = allowed();
  knob.connect(context.destination);
  const steer = () => knob.gain.setTargetAtTime(allowed(), context.currentTime, VOLUME_TAU_S);
  const attach = () => {
    if (!loaded || limiter || !quiet()) return;
    knob.disconnect();
    knob.connect(loaded);
    loaded.connect(context.destination);
    limiter = loaded;
    steer(); // a boost that was held at 100% can go through now
  };
  // Through a resolved promise so a synchronous throw (no audioWorklet at all,
  // as on a page Chromium does not count as secure) lands in the same catch
  // as a failed load: thrown here, it would take her whole voice down with
  // it instead of only the boost.
  const ready = Promise.resolve()
    .then(() => context.audioWorklet.addModule(new URL("./voice-worklet.js", import.meta.url)))
    .then(() => {
      // Mono in, mono out: her voice is one channel, and the speaker upmixes.
      loaded = new AudioWorkletNode(context, "bilisama-voice-limiter", {
        channelCount: 1,
        channelCountMode: "explicit",
        outputChannelCount: [1],
      });
      attach();
    })
    .catch((err) => onTrouble(err));
  return {
    input: knob,
    /** Settles once the limiter has loaded (or failed to); never rejects. */
    ready,
    /** 1 = as she arrived, 0 = silent; above 1 only once the limiter is in. */
    setVolume(gain) {
      wanted = gain;
      steer();
    },
    attach,
    limited: () => limiter !== null,
  };
}

export function createAudio({ onOwner, onNotice = () => {} }) {
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
  let playedMs = 0; // legacy audio without a reply ID
  let receivingReply = null;
  const replies = new Map();
  const settled = new Set();
  const rememberSettled = (id) => {
    settled.add(id);
    if (settled.size > 256) settled.delete(settled.values().next().value);
  };
  let analyser = null; // input level, for the panel's meter
  let wantedInput = undefined; // deviceId the streamer picked, if any
  let voice = null; // createVoiceChain, built with the context
  let volume = 1; // her volume from panel.state, kept for a context not built yet
  // Bumped by every stopCapture(), so a getUserMedia still in flight can tell
  // on the way back that the chain it was opening is no longer wanted.
  let captureGen = 0;

  // One context per page, and her voice chain is built with it: every path
  // that might be first (capture, her first word, a speaker switch, the test
  // tone) comes through here, so no buffer can reach a context without one.
  const ensureContext = () => {
    if (context) return context;
    context = new AudioContext();
    voice = createVoiceChain(context, {
      volume,
      quiet: () => live.length === 0,
      onTrouble: (err) => {
        console.warn("防爆音保护没装上：", err);
        onNotice(
          `防爆音保护没装上（${err}），这一场她的音量最高只到 100%，调小照常。` +
            "刷新页面或重开桌宠再试。",
        );
      },
    });
    return context;
  };

  const role = window.bilisamaShell ? "shell" : "browser";
  const url =
    `ws://${location.host}${location.pathname.replace(/\/$/, "")}` + `/audio?role=${role}`;

  const open = () => {
    if (closed) return;
    // One open() is one connection, whoever called. The backoff timer and
    // retry() used to fire independently — the server releases the devices a
    // millisecond after the page's own close and broadcasts audio.owner{null},
    // retry() takes the second socket, and then the timer nobody cancelled
    // opened a third that the server refused (4409). Measured: 3 sockets from
    // one plain disconnect.
    clearTimeout(retryTimer);
    retryTimer = null;
    // Local, not the module binding: every callback below belongs to THIS
    // connection. A stale one running the shared teardown is how a refused
    // third socket used to stop the second one's microphone.
    const ws = new WebSocket(url);
    socket = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      if (socket !== ws) {
        ws.close(); // superseded while connecting; do not leave it half-alive
        return;
      }
      attempt = 0;
      onOwner(role);
      startCapture();
    };
    ws.onmessage = (message) => {
      if (socket !== ws) return;
      if (typeof message.data === "string") {
        // A stop, ordered behind every sample it is meant to stop. The control
        // socket says the same thing, but that is a different connection —
        // audio the server had already queued would arrive after that clear
        // and start playing again, which is the beat of voice the streamer
        // heard carrying on after the text had stopped.
        try {
          const { event, data = {} } = JSON.parse(message.data);
          if (event === "playback.clear") stopEverything(data);
          else if (event === "playback.reply") {
            receivingReply = Number.isInteger(data.handle_id) ? data.handle_id : null;
          } else if (event === "playback.reply_done") {
            const reply = replies.get(data.handle_id);
            if (reply) {
              reply.done = true;
              finishReply(data.handle_id);
            }
          }
        } catch {
          // Not ours; a socket that only ever carries our own frames should
          // still not die on one it cannot read.
        }
        return;
      }
      schedule(new Int16Array(message.data));
    };
    ws.onclose = (event) => {
      // Only for the connection we are actually on. stopCapture() is shared,
      // and running it for a socket that was already replaced tears down the
      // live one's capture chain.
      if (socket !== ws) return;
      stopEverything({ reason: "audio_handoff" });
      stopCapture();
      onOwner(null);
      if (closed) return;
      if (event.code === 4409 || event.code === 1001) {
        // Someone stronger has the devices (4409 at the door, 1001 when we
        // were displaced holding them). Either way the answer will not change
        // by asking again, and the control socket's audio.owner broadcast is
        // what tells the panel what happened.
        return;
      }
      const delay = Math.min(8000, 500 * 2 ** attempt) * (0.7 + Math.random() * 0.6);
      attempt += 1;
      retryTimer = setTimeout(open, delay);
    };
    ws.onerror = () => ws.close();
  };

  async function startCapture() {
    if (capture) return;
    // `if (capture) return` cannot hold across the two awaits below: capture is
    // only assigned on the last line, and getUserMedia alone runs from tens of
    // milliseconds on a warm device to seconds on a Bluetooth headset changing
    // profile or a first permission prompt. Anything that happened meanwhile —
    // a 4409 refusal, a second use_input, a dropped socket — moves the
    // generation on, and this attempt hands its microphone back instead of
    // leaving a live capture nobody can reach (the invariant AudioBroker.claim
    // states for its `close` argument, ui/audio.py) or a second chain feeding
    // the same socket.
    const mine = (captureGen += 1);
    const superseded = () =>
      captureGen !== mine || socket === null || socket.readyState !== WebSocket.OPEN;
    const letGo = (granted) => granted.getTracks().forEach((track) => track.stop());
    let granted = null;
    try {
      granted = await navigator.mediaDevices.getUserMedia({
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
      // this socket holds the claim. A superseded attempt says nothing: the
      // window that replaced it is the one with news.
      if (superseded()) return null;
      console.warn("拿不到麦克风：", err);
      onOwner(role, String(err));
      // Handed back rather than thrown: the other caller is ws.onopen, where a
      // rejection has nowhere to land. useInput() turns it into a throw.
      return err;
    }
    if (superseded()) {
      letGo(granted);
      return null;
    }
    ensureContext();
    await context.audioWorklet.addModule(new URL("./capture-worklet.js", import.meta.url));
    if (superseded()) {
      letGo(granted);
      return;
    }
    stream = granted;
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
    return null;
  }

  function stopCapture() {
    // Anything still being granted belongs to a chain nobody wants now.
    captureGen += 1;
    capture?.source.disconnect();
    capture?.node.disconnect();
    capture?.mute.disconnect();
    capture?.node.port.close();
    capture = null;
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
  }

  function finishReply(id) {
    const reply = replies.get(id);
    if (!reply || !reply.done || reply.pending !== 0 || !reply.started) return;
    report("playback.reply_ended", { handle_id: id, played_ms: Math.round(reply.playedMs) });
    replies.delete(id);
    rememberSettled(id);
  }

  function observeStart(entry) {
    const reply = replies.get(entry.id);
    if (!reply || reply.started || !live.includes(entry)) return;
    // Scheduling a source is not playback: the AudioContext can be suspended,
    // and earlier audio may still occupy the speaker. Read its running clock.
    if (context.currentTime < entry.start) {
      entry.timer = setTimeout(() => observeStart(entry), 10);
      return;
    }
    reply.started = true;
    report("playback.reply_started", { handle_id: entry.id });
  }

  function finishSegment(entry) {
    // A stop can settle a completed buffer before its queued onended runs.
    if (!live.includes(entry)) return;
    const reply = replies.get(entry.id);
    if (reply && !reply.started) observeStart(entry);
    clearTimeout(entry.timer);
    entry.source.onended = null;
    live = live.filter((item) => item !== entry);
    if (reply) {
      reply.pending -= 1;
      reply.playedMs += entry.duration * 1000;
    } else playedMs += entry.duration * 1000;
    if (!live.length) voice.attach();
    report("playback.ended", entry.id === null ? {} : { handle_id: entry.id });
    if (reply) finishReply(entry.id);
  }

  function stopEverything({ reply_ids: ids = [], reason = "barge_in" } = {}) {
    const targets = new Set(ids.filter(Number.isInteger));
    const all = targets.size === 0;
    const now = context ? context.currentTime : 0;
    const completed = new Set();
    for (const [id, reply] of replies) {
      if (!reply.done || (!all && !targets.has(id))) continue;
      const remaining = live.filter((entry) => entry.id === id);
      if (remaining.every((entry) => entry.start + entry.duration <= now)) {
        completed.add(id);
        remaining.forEach(finishSegment);
        finishReply(id);
      }
    }
    const stopping = live.filter((entry) => all || targets.has(entry.id));
    const heard = new Map();
    for (const [id, reply] of replies) {
      if (all || targets.has(id)) heard.set(id, reply.playedMs);
    }
    let legacyHeard = playedMs;
    for (const entry of stopping) {
      const partialMs = Math.max(0, Math.min(now - entry.start, entry.duration)) * 1000;
      if (entry.id === null) legacyHeard += partialMs;
      else heard.set(entry.id, (heard.get(entry.id) ?? 0) + partialMs);
      clearTimeout(entry.timer);
      entry.source.onended = null;
      try {
        entry.source.stop();
      } catch {
        // Already finished between the clear and this loop.
      }
    }
    live = live.filter((entry) => !stopping.includes(entry));
    nextStart = live.length ? Math.max(...live.map((entry) => entry.start + entry.duration)) : 0;
    if (all) playedMs = 0;
    for (const id of targets) {
      // A clear can overtake the first PCM on the control socket. Retire the
      // ID now so that late audio cannot start playing again.
      if (!heard.has(id)) heard.set(id, 0);
    }
    for (const [id, duration] of heard) {
      if (!settled.has(id)) {
        report("playback.cancelled", { handle_id: id, played_ms: Math.round(duration), reason });
        rememberSettled(id);
      }
      replies.delete(id);
    }
    if (
      all &&
      (stopping.some((entry) => entry.id === null) || (heard.size === 0 && completed.size === 0))
    ) {
      report("playback.cancelled", { played_ms: Math.round(legacyHeard), reason });
    }
    voice?.attach();
  }

  function schedule(samples) {
    if (!samples.length || settled.has(receivingReply)) return;
    const id = receivingReply;
    if (id !== null && !replies.has(id)) {
      replies.set(id, { pending: 0, started: false, done: false, playedMs: 0 });
    }
    const reply = replies.get(id);
    ensureContext();
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
    source.connect(voice.input);
    // A small cushion the first time: starting exactly at currentTime means
    // any scheduling jitter lands in the past and the browser drops the head
    // of the first word.
    const start = Math.max(nextStart, context.currentTime + 0.05);
    source.start(start);
    nextStart = start + buffer.duration;
    const entry = { source, start, duration: buffer.duration, id, timer: null };
    live.push(entry);
    if (reply) {
      reply.pending += 1;
      if (reply.pending === 1 && !reply.started) observeStart(entry);
    }
    // Per SEGMENT, both of them. One reply plays as many buffers, and the
    // instant between two of them is not the end of speaking — reading it as
    // one is how the backlog comes back (ledger #41).
    const receipt = id === null ? {} : { handle_id: id };
    report("playback.started", receipt);
    source.onended = () => finishSegment(entry);
  }

  open();

  return {
    /** Answer one panel request. Only the device holder ever gets here.
     *
     * The panel cannot call these directly: inside the shell it is a separate
     * window from the one with the microphone, so the request comes over the
     * wire and the answer goes back the same way.
     */
    async command(request, reply) {
      // Every path answers, and nothing here is allowed to reject. The caller
      // is a frame dispatcher with nowhere to put an error, so a throw used to
      // land as an unhandled rejection: the reply never went out, the dropdown
      // sat on a device that was not playing anything, and the panel said
      // nothing at all. Unplugging a selected device is the ordinary way in.
      const what = request?.what;
      try {
        if (what === "devices") {
          reply({ kind: "devices", devices: await this.devices() });
        } else if (what === "use_input") {
          await this.useInput(request.id);
          reply({ kind: "devices", devices: await this.devices() });
        } else if (what === "use_output") {
          const moved = await this.useOutput(request.id);
          reply({ kind: "devices", moved, devices: await this.devices() });
        } else if (what === "test") {
          await this.test();
        } else if (what === "level") {
          reply({ kind: "level", level: this.level() });
        }
      } catch (err) {
        console.warn("音频命令失败：", what, err);
        // Say what broke AND re-send the list, so the panel can put the
        // dropdown back on whatever is actually playing.
        let devices = [];
        try {
          devices = await this.devices();
        } catch {
          // Enumeration is failing too; an empty list still beats silence.
        }
        reply({ kind: "devices", devices, error: String(err) });
      }
    },
    /** Ask again after standing aside; ignored while already connected.
     *
     * A displaced window stops knocking, which is right while something
     * stronger holds the devices and wrong the moment it lets go — otherwise
     * closing the shell leaves every open page silent until reloaded, with
     * nothing on screen to explain it. The control socket's audio.owner
     * broadcast is the cue.
     */
    retry() {
      if (closed) return;
      if (socket && socket.readyState <= WebSocket.OPEN) return;
      attempt = 0;
      open();
    },
    /** Stop everything scheduled and report how much of it was heard. */
    clear: stopEverything,
    /** Her volume from panel.state, in percent (100 = as she arrived).
     *
     * Anything that is not a number a slider could produce is ignored rather
     * than guessed at: the frame may come from an older server with no such
     * field, and silence or a jump to 100 would both be wrong.
     */
    setVolume(percent) {
      if (typeof percent !== "number" || !Number.isFinite(percent) || percent < 0) return;
      volume = percent / 100;
      voice?.setVolume(volume);
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
      const failure = await startCapture();
      // Rethrown so command()'s catch runs. Without this the panel got a plain
      // device list back, put the dropdown on the microphone it had asked for,
      // and said nothing — while nothing was being captured at all. Picking a
      // device that is gone or blocked is the ordinary way in.
      if (failure) throw failure;
    },
    /** Move playback to a different speaker.
     *
     * This does NOT cost the echo cancellation, which is the opposite of what
     * an earlier version of this comment claimed. Chromium moves the AEC
     * reference along with the sink: setSinkId marks the destination, and its
     * next start calls SetOutputDeviceForAec with the new id
     * (realtime_audio_destination_handler.cc:419-441, read at tag
     * 150.0.7871.224 — the Chromium inside Electron 43.4.0). Older write-ups
     * say otherwise because it used to be true; crbug 40252911 is the fix.
     *
     * What the canceller genuinely cannot subtract is audio some OTHER process
     * renders — the reference is Chromium's own playback to one device
     * (media_switches.cc:506-509). OBS monitoring, a game, background music:
     * none of it is in there, whatever this control is set to.
     */
    async useOutput(deviceId) {
      ensureContext();
      if (typeof context.setSinkId !== "function") return false;
      await context.setSinkId(deviceId || "");
      return true;
    },
    /** A short tone down the real playback path, to confirm the right speaker. */
    async test() {
      ensureContext();
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
