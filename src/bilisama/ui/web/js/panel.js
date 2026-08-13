// The control panel: live (health + speak switches + inject), chat timeline,
// log stream, read-only config. It consumes event.feed / log.line /
// panel.state frames routed from main.js and pulls /health and /config over
// plain fetch — health only while someone is actually looking.

import { VISUAL_LABEL } from "./presentation.js";

const FEED_CAP = 200;
const LOG_CAP = 500;
const LOG_RANK = { debug: 0, info: 1, warning: 2, error: 3 };

const SPEAK_LABEL = {
  danmaku: "普通弹幕",
  gift: "礼物",
  super_chat: "SC",
  guard_buy: "上舰",
  vip_enter: "VIP 进房",
  entry: "批量欢迎",
  follow: "关注",
  like: "点赞",
  share: "分享",
  proactive: "主动话题",
  background_result: "后台结果",
};

const FEED_WHO = { sc: "SC", gift: "礼物", danmaku: "弹幕" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clock(ts) {
  return typeof ts === "string" && ts.length >= 19 ? ts.slice(11, 19) : "";
}

export function createPanel({ send }) {
  const panel = document.getElementById("panel");
  const scrim = document.getElementById("scrim");
  const corner = document.getElementById("corner");
  const nameEl = document.getElementById("p-name");
  const stateEl = document.getElementById("p-state");
  const panicBtn = document.getElementById("p-panic");
  const healthEl = document.getElementById("health");
  const matrixEl = document.getElementById("speak-matrix");
  const timelineEl = document.getElementById("timeline");
  const loglinesEl = document.getElementById("loglines");
  const levelSel = document.getElementById("log-level");
  const pauseBtn = document.getElementById("log-pause");
  const injectForm = document.getElementById("inject");
  const injectInput = document.getElementById("inject-input");

  const panelOnly = document.body.classList.contains("panel-only");
  let isOpen = panelOnly;
  let panicked = false;
  let healthTimer = null;
  let configLoaded = false;
  let logPaused = false;

  // ------------------------------------------------------------ open/close

  const refreshHealth = async () => {
    try {
      const snapshot = await (await fetch("health")).json();
      healthEl.textContent = "";
      for (const [name, data] of Object.entries(snapshot.components ?? {})) {
        const card = el("div", "card" + (data && data.error ? " err" : ""));
        card.appendChild(el("div", "card-name", name));
        const kv = el("div", "kv");
        kv.textContent = Object.entries(data ?? {})
          .slice(0, 4)
          .map(([k, v]) => `${k}=${typeof v === "object" && v !== null ? JSON.stringify(v) : v}`)
          .join(" ");
        card.appendChild(kv);
        healthEl.appendChild(card);
      }
    } catch {
      healthEl.textContent = "";
      healthEl.appendChild(el("p", "empty", "健康接口暂时拿不到"));
    }
  };

  const startHealth = () => {
    if (healthTimer) return;
    refreshHealth();
    healthTimer = setInterval(refreshHealth, 5000);
  };

  const stopHealth = () => {
    clearInterval(healthTimer);
    healthTimer = null;
  };

  const open = () => {
    isOpen = true;
    panel.classList.add("open");
    scrim.hidden = false;
    requestAnimationFrame(() => scrim.classList.add("open"));
    startHealth();
    if (!configLoaded) loadConfig();
  };

  const close = () => {
    if (panelOnly) return;
    isOpen = false;
    panel.classList.remove("open");
    scrim.classList.remove("open");
    setTimeout(() => {
      if (!isOpen) scrim.hidden = true;
    }, 200);
    stopHealth();
  };

  corner.addEventListener("click", () => {
    // Inside the desktop shell the panel gets its own real window; in a
    // browser tab it slides in as a sheet.
    if (window.bilisamaShell?.openPanel) {
      window.bilisamaShell.openPanel();
    } else {
      open();
    }
  });
  scrim.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen) close();
  });
  if (panelOnly) {
    startHealth();
    loadConfig();
  }

  // ------------------------------------------------------------ tabs

  for (const tab of panel.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of panel.querySelectorAll(".tab")) other.classList.remove("active");
      for (const page of panel.querySelectorAll(".tab-page")) page.classList.remove("active");
      tab.classList.add("active");
      document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
    });
  }

  // ------------------------------------------------------------ live tab

  panicBtn.addEventListener("click", () => {
    send("panel.set", { panic_mute: !panicked });
  });

  const renderSpeak = (speak) => {
    matrixEl.textContent = "";
    for (const [key, value] of Object.entries(speak ?? {})) {
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.checked = Boolean(value);
      box.addEventListener("change", () => {
        send("panel.set", { speak: { [key]: box.checked } });
      });
      label.appendChild(box);
      label.appendChild(el("span", "", SPEAK_LABEL[key] ?? key));
      matrixEl.appendChild(label);
    }
  };

  injectForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = injectInput.value.trim();
    if (!text) return;
    send("console.line", { text });
    injectInput.value = "";
  });

  // ------------------------------------------------------------ chat tab

  const pushEntry = (node) => {
    timelineEl.querySelector(".empty")?.remove();
    timelineEl.appendChild(node);
    while (timelineEl.children.length > FEED_CAP) timelineEl.firstChild.remove();
    timelineEl.parentElement.scrollTop = timelineEl.parentElement.scrollHeight;
  };

  const feedEntry = (data) => {
    const kind = data.kind ?? "system";
    const entry = el("div", `entry ${kind}`);
    entry.appendChild(el("span", "when", clock(data.ts)));
    if (kind === "verdict") {
      const reason = data.reason ? `(${data.reason})` : "";
      entry.appendChild(
        el("span", "", `${data.source} → ${data.outcome}@${data.phase}${reason}`),
      );
    } else if (kind === "reply") {
      entry.appendChild(el("span", "who", "她"));
      const status = data.status === "completed" ? "" : `〔${data.status}〕`;
      entry.appendChild(el("span", "", `${data.text || "（无文本）"}${status}`));
    } else if (kind === "transcript") {
      entry.appendChild(el("span", "who", "你"));
      entry.appendChild(el("span", "", data.text ?? ""));
    } else if (kind === "sc" || kind === "gift" || kind === "danmaku") {
      const money = data.value_cny ? ` ¥${Math.round(data.value_cny)}` : "";
      entry.appendChild(el("span", "who", `${FEED_WHO[kind]}·${data.name ?? "?"}${money}`));
      entry.appendChild(el("span", "", data.text ?? ""));
    } else if (kind === "error") {
      entry.appendChild(el("span", "", `${data.code ?? "error"}: ${data.detail ?? ""}`));
    } else {
      entry.appendChild(el("span", "", data.text ?? ""));
    }
    pushEntry(entry);
  };

  // ------------------------------------------------------------ logs tab

  const logEntry = (line) => {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      record = { level: "info", event: line };
    }
    const rank = LOG_RANK[record.level] ?? 1;
    const node = el("div", `logline ${record.level ?? "info"}`);
    node.dataset.rank = String(rank);
    const rest = Object.entries(record)
      .filter(([k]) => !["ts", "level", "event", "logger"].includes(k))
      .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
      .join(" ");
    node.textContent = `${clock(record.ts ?? "")} ${record.level ?? ""} ${record.event ?? ""} ${rest}`;
    node.hidden = rank < (LOG_RANK[levelSel.value] ?? 1);
    loglinesEl.appendChild(node);
    while (loglinesEl.children.length > LOG_CAP) loglinesEl.firstChild.remove();
    if (!logPaused) loglinesEl.scrollTop = loglinesEl.scrollHeight;
  };

  levelSel.addEventListener("change", () => {
    const threshold = LOG_RANK[levelSel.value] ?? 1;
    for (const node of loglinesEl.children) {
      node.hidden = Number(node.dataset.rank ?? 1) < threshold;
    }
  });

  pauseBtn.addEventListener("click", () => {
    logPaused = !logPaused;
    pauseBtn.dataset.paused = String(logPaused);
    pauseBtn.textContent = logPaused ? "继续滚动" : "暂停滚动";
    if (!logPaused) loglinesEl.scrollTop = loglinesEl.scrollHeight;
  });

  // ------------------------------------------------------------ config tab

  async function loadConfig() {
    configLoaded = true;
    const listEl = document.getElementById("config-list");
    let rows;
    try {
      rows = await (await fetch("config")).json();
    } catch {
      listEl.textContent = "";
      listEl.appendChild(el("p", "empty", "配置接口暂时拿不到"));
      configLoaded = false;
      return;
    }
    listEl.textContent = "";
    const advanced = el("details");
    advanced.appendChild(el("summary", "", "高级（开发者字段）"));
    // Group headers, tracked per container (main list vs the advanced fold).
    let currentGroup = null;
    let advancedGroup = null;
    for (const row of rows) {
      const isDev = row.audience === "developer";
      const host = isDev ? advanced : listEl;
      if (isDev) {
        if (row.group !== advancedGroup) {
          advancedGroup = row.group;
          host.appendChild(el("h5", "", row.group || "其他"));
        }
      } else if (row.group !== currentGroup) {
        currentGroup = row.group;
        host.appendChild(el("h5", "", row.group || "其他"));
      }
      if (row.value === null) continue; // section-header rows carry no value
      const line = el("div", "cfg-row");
      line.appendChild(el("span", "cfg-label", row.label));
      const unit = row.unit ? ` ${row.unit}` : "";
      line.appendChild(el("span", "cfg-value", `${row.value}${unit}`));
      host.appendChild(line);
      if (row.hint) host.appendChild(el("p", "cfg-hint", row.hint));
    }
    listEl.appendChild(advanced);
  }

  // ------------------------------------------------------------ frames in

  return {
    handleFrame(event, data) {
      if (event === "event.feed") feedEntry(data);
      else if (event === "log.line") logEntry(data.line ?? "");
      else if (event === "panel.state") {
        panicked = Boolean(data.panicked);
        panicBtn.dataset.panicked = String(panicked);
        panicBtn.textContent = panicked ? "恢复说话" : "紧急闭麦";
        renderSpeak(data.speak);
      }
    },
    setHello(data) {
      nameEl.textContent = data.persona?.name ?? "BiliSama";
      if (data.panel) this.handleFrame("panel.state", data.panel);
    },
    setVisual(visual) {
      stateEl.dataset.visual = visual;
      stateEl.textContent = VISUAL_LABEL[visual] ?? visual;
    },
  };
}
