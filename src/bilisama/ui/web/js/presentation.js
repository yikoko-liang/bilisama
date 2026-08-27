// Visual-state arbitration, client half. The server already folds five polled
// signals into voice.state; this only layers the connection on top (the one
// thing the server cannot report about itself). Same shape as
// qwen-audio-agent's orb arbiter, radically smaller because the server does
// the heavy half.

export function resolveVisual({ connected, voice }) {
  if (!connected) return "offline";
  return voice || "idle";
}

export const VISUAL_LABEL = {
  idle: "在场",
  listening: "听你说",
  thinking: "想一想",
  speaking: "说话中",
  offline: "已离线",
};
