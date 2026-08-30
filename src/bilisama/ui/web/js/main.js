// Assembly. Three views of the same page:
//   default        — pet stage + hover corner + slide-in panel (browser tab)
//   #panel         — panel only, full width (the shell's second window)
//   inside shell   — same as default, but drags move the window and the
//                    corner opens a real panel window (window.bilisamaShell)

import { connect } from "./ws.js";
import { resolveVisual } from "./presentation.js";
import { createRenderer } from "./renderer.js";
import { createBubble } from "./bubble.js";
import { createPanel } from "./panel.js";
import { createAudio } from "./audio.js";

const panelOnly = location.hash === "#panel";
if (panelOnly) document.body.classList.add("panel-only");
if (window.bilisamaShell) document.body.classList.add("shell");

const stage = document.getElementById("stage");
const bubble = createBubble(document.getElementById("bubble"));

const state = { connected: false, voice: "idle" };
let renderer = null;
// Bumped when a frame changes shape. ui/events.py stamps the server half.
const PROTOCOL = 1;

let rendererWanted = null; // the avatar config from hello, mounted lazily
let mountChain = Promise.resolve(); // mounts run one at a time, never overlapped
let everConnected = false;

const send = (event, data) => socket.send(event, data);
const panel = createPanel({ send });

// ---- pet-side quick controls: voice input / voice output / pause / settings.
const inputToggleBtn = document.getElementById("voice-input-toggle");
const outputToggleBtn = document.getElementById("voice-output-toggle");
const pauseToggleBtn = document.getElementById("pause-toggle");
const controlState = { paused: false, input: true, output: true };
let bubbleReplyId = null;

// Which replies show as a pet bubble. Paused: none (nothing new speaks
// anyway, and a bubble over a paused pet reads as a ghost). Voice output on:
// her voice-turn replies and the high-value lanes bubble, the ordinary event
// lanes do not — a bubble per danmaku is noise when the voice already carries
// it. With voice output off, the bubble is the only channel, so everything
// shows. The tier comes from the scheduler's own priority on the frame (a
// small gift tiers down to the danmaku rung server-side; a source list here
// could not see that); the source set is only the fallback for old frames.
// (Softened from yiko, which hid even voice replies behind full voice: on
// this branch the pet IS the primary surface, her speech balloon stays.)
const QUIET_BUBBLE_SOURCES = new Set(["danmaku", "entry", "proactive", "background_result"]);
const QUIET_BUBBLE_BELOW = 40; // Priority.BACKGROUND — everything under it queues quietly
function replyUsesBubble(data) {
  if (panelOnly) return false;
  if (controlState.paused) return false;
  if (!controlState.output) return true;
  const source = data?.source ?? "voice";
  if (source === "voice") return true;
  if (typeof data?.priority === "number") return data.priority >= QUIET_BUBBLE_BELOW;
  return !QUIET_BUBBLE_SOURCES.has(source);
}

function paintControls() {
  const set = (btn, on, onTip, offTip) => {
    if (!btn) return;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.dataset.tooltip = on ? onTip : offTip;
    btn.title = on ? onTip : offTip;
    btn.disabled = false; // panel.state answered; the ack gate lifts
  };
  set(inputToggleBtn, controlState.input, "点击关闭语音输入", "点击打开语音输入");
  set(outputToggleBtn, controlState.output, "点击关闭语音播报", "点击打开语音播报");
  if (pauseToggleBtn) {
    pauseToggleBtn.disabled = false; // the ack gate lifts with every repaint
    pauseToggleBtn.classList.toggle("active", controlState.paused);
    pauseToggleBtn.setAttribute("aria-pressed", String(controlState.paused));
    const tip = controlState.paused ? "恢复伴播助手" : "暂停伴播助手";
    pauseToggleBtn.dataset.tooltip = tip;
    pauseToggleBtn.title = tip;
    // While paused the two voice switches are moot — the transport is down.
    inputToggleBtn?.toggleAttribute("disabled", controlState.paused);
    outputToggleBtn?.toggleAttribute("disabled", controlState.paused);
  }
}

// One shape for all three quick keys: ask, disable until panel.state answers
// (paintControls lifts the gate), toast when the link is down. The pause key
// used to skip the ack gate, so a double-click queued two suspend cycles.
function askToggle(btn, patch, failText) {
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (send("panel.set", patch())) {
      btn.disabled = true; // until panel.state repaints with the truth
    } else {
      bubble.showTransient(failText, 3200);
    }
  });
}
askToggle(
  inputToggleBtn,
  () => ({ audio: { input_enabled: !controlState.input } }),
  "语音连接已断开，输入开关没有生效。",
);
askToggle(
  outputToggleBtn,
  () => ({ audio: { output_enabled: !controlState.output } }),
  "语音连接已断开，播报开关没有生效。",
);
askToggle(pauseToggleBtn, () => ({ paused: !controlState.paused }), "语音连接已断开，暂停开关没有生效。");

// Remount only when the avatar actually changed (reconnects and repeated
// state frames keep it). Shared by hello and panel.state: a skin picked on
// the panel rides the state broadcast, no reconnect needed.
function applyAvatar(avatar) {
  if (JSON.stringify(avatar) !== JSON.stringify(rendererWanted)) {
    mountRenderer(avatar);
  }
}

// panel.state is the one truth for all three; the buttons only ASK.
panel.setOnPanelState((state) => {
  const avatar = state.appearance?.avatar;
  if (avatar) applyAvatar(avatar);
  controlState.paused = Boolean(state.paused);
  controlState.input = Boolean(state.audio?.input_enabled ?? true);
  controlState.output = Boolean(state.audio?.output_enabled ?? true);
  paintControls();
  if (controlState.paused) bubble.shatter?.();
});
paintControls();

// ---- the exit handshake: right-click the pet, confirm, app.quit → the
// server broadcasts app.exiting and starts its slow teardown while every
// window closes itself.
const exitDialog = document.getElementById("exit-dialog");
if (!panelOnly && exitDialog) {
  const exitConfirm = document.getElementById("exit-confirm");
  const exitCancel = document.getElementById("exit-cancel");
  const exitConfirmLabel = exitConfirm?.textContent ?? "";
  const closeExitDialog = () => {
    exitDialog.hidden = true;
    // A dismissed quit is a cancelled quit: the confirm button must come
    // back, or the next right-click opens a dialog stuck on 「正在退出…」.
    if (exitConfirm) {
      exitConfirm.disabled = false;
      exitConfirm.textContent = exitConfirmLabel;
    }
    lastFit = ""; // the quitting size no longer applies; shrink back
    fitToSkin();
  };
  document.getElementById("pet-mount")?.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    exitDialog.hidden = false;
    // The shell window normally hugs the pet; the dialog needs real estate.
    lastFit = "";
    fitToSkin();
  });
  exitCancel?.addEventListener("click", closeExitDialog);
  exitDialog.addEventListener("click", (e) => {
    if (e.target === exitDialog) closeExitDialog();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !exitDialog.hidden) closeExitDialog();
  });
  exitConfirm?.addEventListener("click", () => {
    if (send("app.quit")) {
      // Keep the dialog up as the receipt: app.exiting closes the window.
      exitConfirm.disabled = true;
      exitConfirm.textContent = "正在退出…";
    } else if (window.bilisamaShell?.close) {
      // The backend is already gone; nothing will answer. Close what we can.
      window.bilisamaShell.close();
    } else {
      closeExitDialog();
      bubble.showTransient("后端进程没有运行，无需退出。", 3200);
    }
  });
}
// The panel asks over the wire rather than reaching for a module that may
// live in another window: inside the shell, settings and devices are two
// separate windows.
panel.setAsk((payload) => send("audio.ask", payload));

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
    .catch((err) => {
      console.warn("形象挂载失败：", err);
      panel.notice(`形象挂载失败，桌宠这一场可能是空的：${err}`);
    });
  return mountChain;
}

async function mountOne(avatar) {
  if (avatar !== rendererWanted) return; // superseded while it waited its turn
  renderer?.destroy();
  renderer = null;
  renderer = await createRenderer(document.getElementById("pet-mount"), avatar, {
    onPoke: () => send("pet.poke"),
    // A degrade the streamer can read. Inside the shell this lands in THIS
    // window's log tab, which the shell never opens — the panel window is a
    // separate one with no way to hear it. Only what hello carries reaches
    // both (see panel.js's setHello); a skin that failed to load is knowledge
    // this window has alone, and relaying it would need a wire event the
    // server does not have.
    onNotice: (text) => panel.notice(text),
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
  let width = Math.round(Math.max(rect.width + 56, 208));
  let height = Math.round(rect.height + Math.max(BASE_HEADROOM, bubbleH + 40));
  const exitOpen = exitDialog && !exitDialog.hidden;
  if (exitOpen) {
    // The exit card is min(320px, 100%): a 208px pet window would clip it.
    width = Math.max(width, 360);
    height = Math.max(height, 260);
  }
  const key = `${width}x${height}`;
  if (key === lastFit) return; // streaming text resizes by a pixel at a time
  lastFit = key;
  fit(width, height);
}

// Click-through everywhere the pet is not. The shell window is a rectangle
// around a small character, and an invisible pane that still swallows clicks
// is a pane the user can feel — this is the other half of "transparent".
// Only the pet, its bubble and the corner icon take the mouse; the same
// pointer tracking reveals the corner icon, which otherwise floats alone in
// empty space and reads as a smudge on the desktop.
const cornerBtn = document.getElementById("corner");
window.bilisamaShell?.onPanelState?.((open) => {
  cornerBtn?.classList.toggle("active", Boolean(open));
});
// In a plain browser tab the panel slides in-page; the shell IPC above never
// fires, so the corner icon listens to the panel itself.
if (!window.bilisamaShell) {
  panel.setOnOpenChange?.((open) => {
    cornerBtn?.classList.toggle("active", Boolean(open));
    cornerBtn?.setAttribute("aria-pressed", String(Boolean(open)));
  });
}

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
    // The one thing a page cannot work out for itself: whether the code it is
    // running still matches the server that answered. The shell does not hot
    // reload its main process, so a stale window looks completely normal while
    // speaking a vocabulary the server has moved on from — and the symptom is
    // a feature that silently does nothing.
    if (typeof data.protocol === "number" && data.protocol !== PROTOCOL) {
      panel.notice(
        `页面和后端版本对不上（页面 ${PROTOCOL}，后端 ${data.protocol}）。刷新一下；` +
          `如果是桌宠壳，关掉重开——它不会自己热更新。`,
      );
    }
    panel.setHello(data);
    document.title = `${data.persona?.name ?? "BiliSama"} · BiliSama`;
    applyAvatar(data.avatar ?? {});
  },
  "voice.state": (data) => {
    state.voice = data.state ?? "idle";
    applyVisual();
  },
  "reply.delta": (data) => {
    if (!replyUsesBubble(data)) return;
    if (data.reply_id && data.reply_id !== bubbleReplyId) {
      // A different reply started streaming: close the old bubble stream so
      // this one replaces the text instead of appending to it.
      bubbleReplyId = data.reply_id;
      bubble.endReply();
    }
    bubble.delta(data.text ?? "");
  },
  "reply.done": (data) => {
    // The bubble outlives the text stream on purpose; dismissal is keyed to
    // voice.state falling back to idle (see bubble.js). Marking the end is
    // what lets the next reply replace this text instead of appending to it.
    if (replyUsesBubble(data) && data.reply_id && data.reply_id !== bubbleReplyId && data.text) {
      // No delta of this reply ever bubbled (gating flipped mid-reply, or the
      // stream raced the page): show the finished line once instead of never.
      bubbleReplyId = data.reply_id;
      bubble.showTransient(data.text, 4000);
      return;
    }
    bubble.endReply();
  },
  "playback.clear": () => {
    bubble.shatter();
    // Stop what is already scheduled, and report how much of it was heard.
    // The bubble and the sound have to go together — a shattered bubble over a
    // voice that keeps talking is worse than either alone.
    audio?.clear();
  },
  "audio.owner": (data) => {
    panel.handleFrame("audio.owner", data);
    // Devices free again — a window that stood aside can take them.
    if (!data.owner) audio?.retry();
  },
  // Only the window holding the devices can answer these; every other window
  // ignores them, which is what `audio` being null means.
  "audio.command": (data) => audio?.command(data, (report) => send("audio.report", report)),
  "audio.devices": (data) => panel.handleFrame("audio.devices", data),
  "audio.level": (data) => panel.handleFrame("audio.level", data),
  "event.feed": (data) => panel.handleFrame("event.feed", data),
  "log.line": (data) => panel.handleFrame("log.line", data),
  "panel.state": (data) => panel.handleFrame("panel.state", data),
  "transcript.final": () => {
    // Already mirrored into event.feed by the server; the stage shows nothing
    // extra for it today.
  },
  "app.exiting": () => {
    // The backend acknowledged the quit (or someone else asked for it).
    // Close before the socket dies so the window never enters its reconnect
    // loop against a server that is deliberately going away.
    socket.close?.();
    if (window.bilisamaShell?.close) {
      window.bilisamaShell.close();
    } else {
      // A browser tab cannot always close itself; a blank page beats a
      // spinner that reconnects forever.
      location.replace("about:blank");
    }
  },
};

// The panel window carries no pet and no devices: two claims from one shell
// would have them fighting over the same microphone.
const audio = panelOnly
  ? null
  : createAudio({
      // Only the failure is local knowledge — "this window could not get a
      // microphone" is something no broadcast can say for us. Who holds the
      // devices comes back from the server, so both windows agree.
      onOwner: (owner, error) => {
        if (error) panel.setAudioOwner(owner, error);
        // No error means this window has no bad news any more: it got its
        // microphone, or it stood aside for something stronger. Either way the
        // previous refusal must stop being reported against the window that
        // holds the devices now.
        else panel.forgetLocalMicTrouble();
      },
    });

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
