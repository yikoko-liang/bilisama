import { connect } from "./ws.js";

const $ = (selector) => document.querySelector(selector);
const shareButton = $("#share-button");
const shareStop = $("#share-stop");
const preview = $("#share-preview");
const previewWrap = $(".preview-wrap");
const sourceName = $("#source-name");
const audioState = $("#audio-state");
const meterFill = $("#meter-fill");
const roomInput = $("#room-id");
const streamIntro = $("#stream-intro");
const checkButton = $("#check-button");
const startButton = $("#start-button");
const stopButton = $("#stop-button");
const formError = $("#form-error");
const connectionPill = $("#connection-pill");
const preflightSummary = $("#preflight-summary");
const startBox = $(".start-box");
const startHint = $("#start-hint");
const eventCount = $("#event-count");
const audioCount = $("#audio-count");
const monitorFeed = $("#monitor-feed");
const flowSteps = [...document.querySelectorAll(".flow-step")];
const eventSwitches = [...document.querySelectorAll("[data-speak]")];

let stream = null;
let audioContext = null;
let audioNode = null;
let audioFramesCaptured = 0;
let lastPeak = 0;
let connected = false;
let backendState = null;
let monitorStarted = false;
let configuredRoomId = 0;
let configuredStreamIntro = "";
let streamIntroEdited = false;

const socket = connect({
  onFrame(event, data) {
    if (event === "hello") {
      applySpeak(data.panel?.speak);
      applyRoomConfig(data.panel?.room);
      applyState(data.live_mock ?? null);
      return;
    }
    if (event === "live_mock.state") applyState(data);
    else if (event === "live_mock.event") appendEntry(data.kind, data.name, eventText(data));
    else if (event === "transcript.final") appendEntry("transcript", "浏览器语音", data.text ?? "");
    else if (event === "reply.done" && data.text) {
      const reference = data.reference ? `引用${eventLabel(data.reference.kind)}：${eventText(data.reference)} ｜ ` : "";
      appendEntry("reply", "伴播", `${reference}${data.text}`);
    }
    else if (event === "panel.state") applySpeak(data.speak);
  },
  onStatus(value) {
    connected = value;
    connectionPill.dataset.state = value ? "online" : "offline";
    connectionPill.textContent = value ? "伴播后端已连接" : "后端连接中";
    render();
  },
});

function eventText(data) {
  const identity = [
    data.identity && data.identity !== "anon" ? data.identity : "",
    Number(data.user_level) > 0 ? `用户 Lv.${data.user_level}` : "",
    Number(data.wealth_level) > 0 ? `财富 Lv.${data.wealth_level}` : "",
    data.is_admin ? "房管" : "",
    ({ governor: "总督", admiral: "提督", captain: "舰长" })[data.guard_level] ?? "",
    data.medal?.name ? `${data.medal.this_room ? "本房" : "外房"}粉丝牌 ${data.medal.name} Lv.${data.medal.level}` : "",
  ].filter(Boolean).join(" · ");
  const gift = data.gift?.name
    ? `${data.gift.name} ×${data.gift.num ?? 1} · ${data.gift.total_battery ?? 0} 电池`
    : "";
  const rawAmount = Number(data.value_cny) > 0 ? `原始金额 ¥${Number(data.value_cny).toFixed(2)}` : "";
  return [identity, data.text, gift, rawAmount].filter(Boolean).join(" ｜ ");
}

function eventLabel(kind) {
  return ({
    danmaku: "弹幕", gift: "礼物", super_chat: "SC", guard_buy: "上舰",
    vip_enter: "VIP 进房", entry: "进房", transcript: "主播语音",
  })[kind] ?? kind ?? "事件";
}

function captureSnapshot() {
  const videoTrack = stream?.getVideoTracks()[0];
  const audioTrack = stream?.getAudioTracks()[0];
  return {
    video_live: videoTrack?.readyState === "live",
    audio_live: audioTrack?.readyState === "live" && audioFramesCaptured > 0,
    source_label: videoTrack?.label || sourceName.textContent || "浏览器共享源",
    sample_rate: audioContext?.sampleRate ?? 0,
  };
}

async function chooseShare() {
  formError.textContent = "";
  if (!navigator.mediaDevices?.getDisplayMedia) {
    formError.textContent = "当前窗口不支持屏幕共享，请用 Chrome 打开本页面。";
    return;
  }
  try {
    stopCapture(false);
    const selected = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 15, max: 30 } },
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    stream = selected;
    preview.srcObject = selected;
    previewWrap.dataset.empty = "false";
    shareStop.disabled = false;
    const videoTrack = selected.getVideoTracks()[0];
    const audioTrack = selected.getAudioTracks()[0];
    sourceName.textContent = videoTrack?.label || "已选择共享源";
    for (const track of selected.getTracks()) track.addEventListener("ended", () => stopCapture());
    if (!audioTrack) {
      audioState.textContent = "没有共享音轨";
      formError.textContent = "共享源没有音轨。请重新选择 Chrome 标签页，并勾选‘共享标签页音频’。";
    } else {
      audioState.textContent = "正在检测音轨…";
      await startAudio(selected);
    }
    render();
  } catch (error) {
    if (error?.name !== "NotAllowedError") formError.textContent = `共享启动失败：${error?.message ?? error}`;
    render();
  }
}

async function startAudio(selected) {
  audioContext = new AudioContext({ latencyHint: "interactive" });
  await audioContext.resume();
  const source = audioContext.createMediaStreamSource(new MediaStream(selected.getAudioTracks()));
  const silent = audioContext.createGain();
  silent.gain.value = 0;
  if (audioContext.audioWorklet) {
    await audioContext.audioWorklet.addModule("assets/js/capture-worklet.js");
    audioNode = new AudioWorkletNode(audioContext, "bilisama-capture");
    audioNode.port.onmessage = (message) => consumeFloatAudio(message.data);
  } else {
    audioNode = audioContext.createScriptProcessor(2048, 1, 1);
    audioNode.onaudioprocess = (event) => consumeFloatAudio(event.inputBuffer.getChannelData(0));
  }
  source.connect(audioNode);
  audioNode.connect(silent);
  silent.connect(audioContext.destination);
}

function consumeFloatAudio(samples) {
  audioFramesCaptured += 1;
  let peak = 0;
  for (let i = 0; i < samples.length; i += 1) peak = Math.max(peak, Math.abs(samples[i]));
  lastPeak = Math.max(peak, lastPeak * 0.82);
  meterFill.style.width = `${Math.min(100, Math.round(lastPeak * 180))}%`;
  audioState.textContent = peak > 0.002 ? "音轨有声音" : "音轨已收到，当前安静";
  if (!backendState?.running) {
    render();
    return;
  }
  const pcm = downsampleToPcm16(samples, audioContext.sampleRate, 16000);
  socket.send("audio.chunk", { sample_rate: 16000, pcm16_b64: toBase64(pcm) });
}

function downsampleToPcm16(input, inputRate, outputRate) {
  if (inputRate < outputRate) return new Int16Array(0);
  const ratio = inputRate / outputRate;
  const length = Math.floor(input.length / ratio);
  const output = new Int16Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end && j < input.length; j += 1) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / (end - start)));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output;
}

function toBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function stopCapture(notify = true) {
  if (notify) socket.send("live_mock.capture_stop", {});
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  preview.srcObject = null;
  previewWrap.dataset.empty = "true";
  audioNode?.disconnect();
  audioNode = null;
  audioContext?.close();
  audioContext = null;
  audioFramesCaptured = 0;
  lastPeak = 0;
  meterFill.style.width = "0";
  sourceName.textContent = "未选择共享源";
  audioState.textContent = "等待音轨";
  shareStop.disabled = true;
  render();
}

function runCheck() {
  formError.textContent = "";
  const roomId = Number(roomInput.value.trim());
  if (!Number.isSafeInteger(roomId) || roomId <= 0) {
    formError.textContent = "请输入正确的 B 站房间号。";
    return;
  }
  const capture = captureSnapshot();
  if (!capture.video_live || !capture.audio_live) {
    formError.textContent = capture.video_live
      ? "共享音轨还没收到数据，请确认勾选了‘共享标签页音频’。"
      : "请先选择共享浏览器标签页。";
    return;
  }
  checkButton.disabled = true;
  checkButton.textContent = "检测中…";
  const speak = {};
  for (const input of eventSwitches) {
    if (input.dataset.speak === "entry") {
      speak.entry = input.checked;
      speak.vip_enter = input.checked;
    } else {
      speak[input.dataset.speak] = input.checked;
    }
  }
  socket.send("live_mock.check", {
    room_id: roomId,
    stream_intro: streamIntro.value.trim(),
    speak,
    capture,
  });
}

function applyRoomConfig(room) {
  if (!room) return;
  configuredRoomId = Number(room.room_id) || 0;
  configuredStreamIntro = String(room.stream_intro ?? "");
  if (!roomInput.value && configuredRoomId) roomInput.value = String(configuredRoomId);
  if (!streamIntroEdited) streamIntro.value = configuredStreamIntro;
}

function applyState(state) {
  if (!state) return;
  backendState = state;
  if (state.room_id && !roomInput.value) roomInput.value = String(state.room_id);
  formError.textContent = state.status === "error" ? state.error ?? "状态检测失败" : "";
  render();
}

function applySpeak(speak) {
  for (const input of eventSwitches) {
    if (input.dataset.speak === "entry") {
      input.checked = Boolean(speak?.entry && speak?.vip_enter);
    } else if (Object.hasOwn(speak ?? {}, input.dataset.speak)) {
      input.checked = Boolean(speak[input.dataset.speak]);
    }
  }
}

function render() {
  const checks = backendState?.checks ?? {};
  let passed = 0;
  for (const id of ["backend", "screen", "audio", "room"]) {
    const row = document.querySelector(`[data-check="${id}"]`);
    let item = checks[id];
    if (id === "backend" && !item) item = { ok: connected, detail: connected ? "后端已连接" : "等待连接" };
    if (id === "screen") item = { ...(item ?? {}), ok: captureSnapshot().video_live, detail: captureSnapshot().video_live ? sourceName.textContent : "尚未选择" };
    if (id === "audio") item = { ...(item ?? {}), ok: captureSnapshot().audio_live, detail: captureSnapshot().audio_live ? audioState.textContent : "尚未收到共享音轨" };
    const ok = Boolean(item?.ok);
    passed += Number(ok);
    row.classList.toggle("ok", ok);
    row.querySelector("p").textContent = item?.detail ?? "尚未检测";
  }
  preflightSummary.textContent = `${passed} / 4`;
  preflightSummary.classList.toggle("ready", passed === 4);
  const checking = backendState?.status === "checking";
  checkButton.disabled = checking || !connected;
  checkButton.textContent = checking ? "检测中…" : "检测状态";
  startButton.disabled = !connected || !backendState?.can_start || !captureSnapshot().audio_live;
  stopButton.disabled = !backendState?.running;
  startBox.classList.toggle("running", Boolean(backendState?.running));
  startHint.textContent = backendState?.running
    ? "麦克风已屏蔽；共享音轨和真实直播事件正在进入正式伴播链路。"
    : backendState?.can_start
      ? "状态正常，可以开始。开始后麦克风会被屏蔽。"
      : "四项全部通过后才能开始。";
  eventCount.textContent = String(backendState?.events_forwarded ?? 0);
  audioCount.textContent = String(backendState?.audio_frames ?? 0);
  flowSteps[0].classList.toggle("done", captureSnapshot().audio_live);
  flowSteps[1].classList.toggle("active", backendState?.status === "checking" || backendState?.status === "ready");
  flowSteps[1].classList.toggle("done", backendState?.status === "ready" || backendState?.running);
  flowSteps[2].classList.toggle("active", Boolean(backendState?.running));
}

function appendEntry(kind, who, text) {
  if (!text && !who) return;
  if (!monitorStarted) {
    monitorFeed.textContent = "";
    monitorStarted = true;
  }
  const entry = document.createElement("div");
  entry.className = `monitor-entry ${kind ?? ""}`;
  const name = document.createElement("b");
  name.textContent = who || kind || "事件";
  entry.append(name, document.createTextNode(text ? ` ${text}` : ""));
  monitorFeed.appendChild(entry);
  while (monitorFeed.children.length > 100) monitorFeed.firstChild.remove();
  monitorFeed.scrollTop = monitorFeed.scrollHeight;
}

shareButton.addEventListener("click", chooseShare);
shareStop.addEventListener("click", () => stopCapture());
checkButton.addEventListener("click", runCheck);
roomInput.addEventListener("input", () => {
  const candidate = Number(roomInput.value.trim());
  if (candidate > 0 && candidate !== configuredRoomId && !streamIntroEdited) {
    streamIntro.value = "";
  }
});
streamIntro.addEventListener("input", () => {
  streamIntroEdited = streamIntro.value !== configuredStreamIntro;
});
startButton.addEventListener("click", () => socket.send("live_mock.start", {}));
stopButton.addEventListener("click", () => socket.send("live_mock.stop", {}));
for (const input of eventSwitches) {
  input.addEventListener("change", () => {
    const patch = input.dataset.speak === "entry"
      ? { entry: input.checked, vip_enter: input.checked }
      : { [input.dataset.speak]: input.checked };
    socket.send("panel.set", { speak: patch });
  });
}
window.addEventListener("pagehide", () => socket.send("live_mock.stop", {}));

render();
