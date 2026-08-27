// Assembly. Three views of the same page:
//   default        — pet stage + quick controls + slide-in panel (browser tab)
//   #panel         — panel only, full width (the shell's second window)
//   inside shell   — same as default, but drags move the window and the
//                    settings opens a real panel window (window.bilisamaShell)

import { connect } from "./ws.js";
import { resolveVisual } from "./presentation.js";
import { createRenderer } from "./renderer.js";
import { createBubble } from "./bubble.js";
import { createPanel } from "./panel.js";

const panelOnly = location.hash === "#panel";
if (panelOnly) document.body.classList.add("panel-only");
if (window.bilisamaShell) document.body.classList.add("shell");

const stage = document.getElementById("stage");
const bubble = createBubble(document.getElementById("bubble"));
const inputButton = document.getElementById("voice-input-toggle");
const outputButton = document.getElementById("voice-output-toggle");
const pauseButton = document.getElementById("pause-toggle");
const settingsButton = document.getElementById("corner");
const exitDialog = document.getElementById("exit-dialog");
const exitConfirm = document.getElementById("exit-confirm");

const state = {
  connected: false,
  voice: "idle",
  inputEnabled: true,
  outputEnabled: true,
  paused: false,
};
let currentReply = null;
let renderer = null;
let rendererWanted = null; // the avatar config from hello, mounted lazily
let mountChain = Promise.resolve(); // mounts run one at a time, never overlapped
let everConnected = false;

const send = (event, data) => socket.send(event, data);

function setSettingsOpen(open) {
  settingsButton.classList.toggle("active", open);
  settingsButton.setAttribute("aria-pressed", String(open));
}

const panel = createPanel({ send, onOpenChange: setSettingsOpen });
window.bilisamaShell?.onPanelState?.(setSettingsOpen);

function setAudioButton(button, enabled, enabledText, disabledText) {
  button.classList.toggle("active", enabled);
  button.disabled = false;
  button.setAttribute("aria-pressed", String(enabled));
  const label = enabled ? enabledText : disabledText;
  button.title = label;
  button.dataset.tooltip = label;
  button.setAttribute("aria-label", label);
}

function applyAudioState(audio = {}) {
  if (typeof audio.input_enabled === "boolean") state.inputEnabled = audio.input_enabled;
  if (typeof audio.output_enabled === "boolean") state.outputEnabled = audio.output_enabled;
  setAudioButton(
    inputButton,
    state.inputEnabled,
    "点击关闭语音输入",
    "点击开启语音输入",
  );
  setAudioButton(
    outputButton,
    state.outputEnabled,
    "点击关闭语音播报",
    "点击开启语音播报",
  );
  inputButton.disabled = state.paused;
  outputButton.disabled = state.paused;
}

function applyPauseState(paused) {
  state.paused = Boolean(paused);
  pauseButton.disabled = false;
  pauseButton.classList.toggle("active", state.paused);
  pauseButton.setAttribute("aria-pressed", String(state.paused));
  pauseButton.title = state.paused ? "恢复伴播助手" : "暂停伴播助手";
  pauseButton.dataset.tooltip = pauseButton.title;
  pauseButton.setAttribute("aria-label", pauseButton.title);
  inputButton.disabled = state.paused;
  outputButton.disabled = state.paused;
  if (state.paused) {
    currentReply = null;
    bubble.hide();
  }
}

const BUBBLE_WHILE_LISTENING = new Set(["gift", "super_chat", "vip_enter"]);

function replyUsesBubble(source) {
  if (state.paused) return false;
  if (!state.inputEnabled || !state.outputEnabled) return true;
  return BUBBLE_WHILE_LISTENING.has(source);
}

function receiveReplyDelta(data) {
  const replyId = data.reply_id ?? "implicit";
  const source = data.source ?? "voice";
  if (!currentReply || currentReply.id !== replyId) {
    currentReply = { id: replyId, source, text: "", visible: false };
  }
  currentReply.source = source;
  currentReply.text += data.text ?? "";
  const wanted = replyUsesBubble(source);
  if (!wanted) {
    if (currentReply.visible) bubble.hide();
    currentReply.visible = false;
    return;
  }
  if (!currentReply.visible) {
    bubble.hide();
    bubble.delta(currentReply.text);
    currentReply.visible = true;
    return;
  }
  bubble.delta(data.text ?? "");
}

function finishReply(data) {
  const replyId = data.reply_id ?? "implicit";
  const source = data.source ?? currentReply?.source ?? "voice";
  if (!currentReply || currentReply.id !== replyId) {
    currentReply = { id: replyId, source, text: data.text ?? "", visible: false };
  }
  if (replyUsesBubble(source) && !currentReply.visible && data.text) {
    bubble.delta(data.text);
    currentReply.visible = true;
  }
  if (currentReply.visible) bubble.endReply();
  currentReply = null;
}

// Every skin owns the same #pet-mount element and clears it — on mount AND on
// destroy. Two mounts in flight would each wipe the other's canvas, whichever
// order they finished in, leaving an invisible pet drawing into a detached
// node. So they queue instead of racing, which also makes "destroy the old one
// first" safe: nothing else can be halfway through a mount.
function mountRenderer(avatar) {
  if (panelOnly) return mountChain;
  rendererWanted = avatar;
  mountChain = mountChain
    .then(() => mountOne(avatar))
    // A broken link in the chain would silently stop every later mount.
    .catch((err) => console.warn("形象挂载失败：", err));
  return mountChain;
}

async function mountOne(avatar) {
  if (avatar !== rendererWanted) return; // superseded while it waited its turn
  renderer?.destroy();
  renderer = null;
  renderer = await createRenderer(document.getElementById("pet-mount"), avatar, {
    onPoke: () => {
      bubble.showTransient("喂，戳我干嘛!");
      send("pet.poke");
    },
  });
  renderer.setState(resolveVisual(state));
  fitToSkin();
}

// The mount box follows the skin's natural proportions, and inside the shell
// the WINDOW follows both the mount and the bubble: pet box + whatever the
// bubble currently needs + breathing room, bottom-anchored on the main side so
// the pet stays planted while the top edge moves.
const BASE_HEADROOM = 118; // pet floor + the gap a bubble-less window keeps
let lastFit = "";

function fitToSkin() {
  const canvas = document.querySelector("#pet-mount canvas");
  const aspect =
    canvas && canvas.width > 0 ? canvas.height / canvas.width : 1.18; // CSS robot default
  document.documentElement.style.setProperty("--pet-aspect", String(aspect));
  const fit = window.bilisamaShell?.fit;
  if (!fit) return;
  const rect = document.getElementById("pet-mount").getBoundingClientRect();
  const bubbleEl = document.getElementById("bubble");
  const bubbleH = bubbleEl && !bubbleEl.hidden ? bubbleEl.getBoundingClientRect().height : 0;
  // 208 keeps a 13px line readable; the bubble needs its own height plus the
  // gap it floats above the pet by.
  const quitting = !exitDialog.hidden;
  const width = Math.round(quitting ? 360 : Math.max(rect.width + 144, 248));
  const height = Math.round(
    quitting ? Math.max(260, rect.height + BASE_HEADROOM) : rect.height + Math.max(BASE_HEADROOM, bubbleH + 40),
  );
  const key = `${width}x${height}`;
  if (key === lastFit) return; // streaming text resizes by a pixel at a time
  lastFit = key;
  fit(width, height);
}

// Click-through everywhere the pet is not. The shell window is a rectangle
// around a small character, and an invisible pane that still swallows clicks
// is a pane the user can feel — this is the other half of "transparent".
// Only the pet, its bubble, the quick controls and the exit dialog take the
// mouse. The rest of the transparent window remains click-through.
if (!panelOnly && window.bilisamaShell?.setInteractive) {
  const shell = window.bilisamaShell;
  let interactive = true; // the window starts live: a page that never runs
  // this code must not end up permanently click-through.
  const hits = (el, x, y) => {
    if (!el || el.hidden) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  };
  const update = (x, y) => {
    // Never let go mid-drag: the pointer routinely leaves the pet's box while
    // the window is following it.
    const dragging = document.querySelector(".pet-mount.dragging") !== null;
    const over =
      dragging ||
      ["pet-mount", "bubble", "pet-controls", "exit-dialog", "confirm-dialog"].some((id) =>
        hits(document.getElementById(id), x, y),
      );
    document.body.classList.toggle("pointer-inside", x >= 0 && y >= 0);
    if (over === interactive) return;
    interactive = over;
    shell.setInteractive(over);
  };
  document.addEventListener("mousemove", (e) => update(e.clientX, e.clientY));
  document.addEventListener("mouseleave", () => update(-1, -1));
}

// The bubble grows as she talks, so the window has to follow it live — a cap
// that fits three lines is why long replies had to be scrolled by hand.
if (!panelOnly && window.bilisamaShell && "ResizeObserver" in window) {
  let queued = false;
  new ResizeObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      fitToSkin();
    });
  }).observe(document.getElementById("bubble"));
}

function applyVisual() {
  const visual = resolveVisual(state);
  stage.dataset.visual = visual;
  renderer?.setState(visual);
  panel.setVisual(visual);
  bubble.onVoiceState(visual);
}

const handlers = {
  "hello": (data) => {
    panel.setHello(data);
    document.title = `${data.persona?.name ?? "BiliSama"} · BiliSama`;
    // Remount only when the avatar actually changed (reconnects keep it).
    const avatar = data.avatar ?? {};
    if (JSON.stringify(avatar) !== JSON.stringify(rendererWanted)) {
      mountRenderer(avatar);
    }
    applyAudioState(data.panel?.audio);
    applyPauseState(data.panel?.paused ?? false);
  },
  "voice.state": (data) => {
    state.voice = data.state ?? "idle";
    applyVisual();
  },
  "reply.delta": (data) => {
    if (!panelOnly) receiveReplyDelta(data);
  },
  "reply.done": (data) => {
    // The bubble outlives the text stream on purpose; dismissal is keyed to
    // voice.state falling back to idle (see bubble.js). Marking the end is
    // what lets the next reply replace this text instead of appending to it.
    if (!panelOnly) finishReply(data);
  },
  "playback.clear": () => {
    currentReply = null;
    bubble.shatter();
  },
  "event.feed": (data) => panel.handleFrame("event.feed", data),
  "log.line": (data) => panel.handleFrame("log.line", data),
  "panel.state": (data) => {
    panel.handleFrame("panel.state", data);
    if (typeof data.paused === "boolean") applyPauseState(data.paused);
    applyAudioState(data.audio);
  },
  "audio.level": (data) => panel.handleFrame("audio.level", data),
  "transcript.final": () => {
    // Already mirrored into event.feed by the server; the stage shows nothing
    // extra for it today.
  },
  "app.exiting": () => {
    socket.close();
    if (window.bilisamaShell?.close) {
      window.bilisamaShell.close();
      return;
    }
    // A normal browser tab cannot always close itself. Leaving the served
    // page still removes the waiting UI and prevents a reconnect loop while
    // the backend finishes distillation in the background.
    location.replace("about:blank");
  },
};

const socket = connect({
  onFrame: (event, data) => handlers[event]?.(data),
  onStatus: (connected) => {
    if (connected && everConnected) {
      // The server replays its feed and log rings on every attach; a panel
      // that kept the old rows would show everything twice after a reconnect.
      panel.reset();
    }
    everConnected = everConnected || connected;
    state.connected = connected;
    applyVisual();
  },
});

inputButton.addEventListener("click", () => {
  const wanted = !state.inputEnabled;
  if (send("panel.set", { audio: { input_enabled: wanted } })) {
    inputButton.disabled = true;
  } else {
    bubble.showTransient("语音连接已断开，输入开关没有生效。", 3200);
  }
});

outputButton.addEventListener("click", () => {
  const wanted = !state.outputEnabled;
  if (send("panel.set", { audio: { output_enabled: wanted } })) {
    outputButton.disabled = true;
  } else {
    bubble.showTransient("语音连接已断开，播报开关没有生效。", 3200);
  }
});

pauseButton.addEventListener("click", () => {
  const wanted = !state.paused;
  if (send("panel.set", { paused: wanted })) {
    pauseButton.disabled = true;
  } else {
    bubble.showTransient("语音连接已断开，暂停开关没有生效。", 3200);
  }
});

function setExitDialog(open) {
  exitDialog.hidden = !open;
  exitConfirm.disabled = false;
  exitConfirm.textContent = "退出";
  lastFit = "";
  requestAnimationFrame(fitToSkin);
}

document.getElementById("pet-mount").addEventListener("contextmenu", (event) => {
  event.preventDefault();
  setExitDialog(true);
});

document.getElementById("exit-cancel").addEventListener("click", () => setExitDialog(false));
exitDialog.addEventListener("click", (event) => {
  if (event.target === exitDialog) setExitDialog(false);
});
exitConfirm.addEventListener("click", () => {
  if (send("app.quit")) {
    exitConfirm.disabled = true;
    exitConfirm.textContent = "正在退出…";
    return;
  }
  setExitDialog(false);
  if (window.bilisamaShell?.close) window.bilisamaShell.close();
  else bubble.showTransient("后端进程没有运行，无需退出。", 3200);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !exitDialog.hidden) setExitDialog(false);
});
