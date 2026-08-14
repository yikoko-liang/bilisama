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

const panelOnly = location.hash === "#panel";
if (panelOnly) document.body.classList.add("panel-only");
if (window.bilisamaShell) document.body.classList.add("shell");

const stage = document.getElementById("stage");
const bubble = createBubble(document.getElementById("bubble"));

const state = { connected: false, voice: "idle" };
let renderer = null;
let rendererWanted = null; // the avatar config from hello, mounted lazily
let mountChain = Promise.resolve(); // mounts run one at a time, never overlapped
let everConnected = false;

const send = (event, data) => socket.send(event, data);
const panel = createPanel({ send });

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
    onPoke: () => send("pet.poke"),
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
  const width = Math.round(Math.max(rect.width + 56, 208));
  const height = Math.round(rect.height + Math.max(BASE_HEADROOM, bubbleH + 40));
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
      ["pet-mount", "bubble", "corner"].some((id) => hits(document.getElementById(id), x, y));
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
  },
  "voice.state": (data) => {
    state.voice = data.state ?? "idle";
    applyVisual();
  },
  "reply.delta": (data) => {
    if (!panelOnly) bubble.delta(data.text ?? "");
  },
  "reply.done": () => {
    // The bubble outlives the text stream on purpose; dismissal is keyed to
    // voice.state falling back to idle (see bubble.js). Marking the end is
    // what lets the next reply replace this text instead of appending to it.
    bubble.endReply();
  },
  "playback.clear": () => {
    bubble.shatter();
  },
  "event.feed": (data) => panel.handleFrame("event.feed", data),
  "log.line": (data) => panel.handleFrame("log.line", data),
  "panel.state": (data) => panel.handleFrame("panel.state", data),
  "transcript.final": () => {
    // Already mirrored into event.feed by the server; the stage shows nothing
    // extra for it today.
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
