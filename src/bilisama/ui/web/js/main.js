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

const send = (event, data) => socket.send(event, data);
const panel = createPanel({ send });

async function mountRenderer(avatar) {
  if (panelOnly) return;
  rendererWanted = avatar;
  renderer?.destroy();
  renderer = await createRenderer(document.getElementById("pet-mount"), avatar, {
    onPoke: () => send("pet.poke"),
  });
  renderer.setState(resolveVisual(state));
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
    // voice.state falling back to idle (see bubble.js).
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
    state.connected = connected;
    applyVisual();
  },
});

// A visible starting point before the first hello lands.
applyVisual();
