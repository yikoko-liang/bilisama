// C4 smoke page: prove the wire works end to end. C6 replaces this wholesale.

const token = location.pathname.split("/").filter(Boolean)[0];
const state = document.getElementById("state");

const show = (text, kind) => {
  state.textContent = text;
  state.dataset.state = kind;
};

const ws = new WebSocket(`ws://${location.host}/${token}/ws`);
ws.onopen = () => show("已连接，等待她上线…", "connected");
ws.onclose = () => show("连接断开", "closed");
ws.onmessage = (message) => {
  const frame = JSON.parse(message.data);
  if (frame.event === "hello") {
    show(`已连接：${frame.data.persona?.name ?? "BiliSama"}`, "connected");
  } else if (frame.event === "voice.state") {
    const label = { idle: "在场", listening: "听你说", thinking: "想一想", speaking: "说话中" };
    show(label[frame.data.state] ?? frame.data.state, frame.data.state);
  }
};
