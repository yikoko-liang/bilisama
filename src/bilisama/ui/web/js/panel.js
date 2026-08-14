// The control panel: live (health + speak switches + inject), chat timeline,
// log stream, read-only config. It consumes event.feed / log.line /
// panel.state frames routed from main.js and pulls /health and /config over
// plain fetch — health only while someone is actually looking.

import { VISUAL_LABEL } from "./presentation.js";

const FEED_CAP = 200;
const LOG_CAP = 500;
// critical must outrank error: without an entry it fell back to info's 1 and
// the worst line in the stream was the one hidden by the "warning 及以上" filter.
const LOG_RANK = { debug: 0, info: 1, warning: 2, error: 3, critical: 4 };

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

  let healthInFlight = false;

  const refreshHealth = async () => {
    if (healthInFlight) return; // a hung endpoint must not stack requests
    healthInFlight = true;
    try {
      const snapshot = await (
        await fetch("health", { signal: AbortSignal.timeout(4000) })
      ).json();
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
    } finally {
      healthInFlight = false;
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
    // force: the tab is only as trustworthy as its last fetch, and switches
    // move from the live tab, from another window, and across sessions.
    loadConfig(true);
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
    // Nothing to repaint while closed; open() refetches anyway.
    clearTimeout(configReloadTimer);
    clearTimeout(configRetryTimer);
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
  // The panel-only window's kick-off lives at the bottom of this factory: it
  // calls loadConfig, whose state is declared further down (a `let` read
  // before its declaration is a ReferenceError, not undefined).

  // ------------------------------------------------------------ tabs

  const tabs = [...panel.querySelectorAll(".tab")];

  const activateTab = (tab) => {
    for (const other of tabs) {
      other.classList.remove("active");
      other.setAttribute("aria-selected", "false");
    }
    for (const page of panel.querySelectorAll(".tab-page")) page.classList.remove("active");
    tab.classList.add("active");
    tab.setAttribute("aria-selected", "true");
    const page = document.getElementById(`tab-${tab.dataset.tab}`);
    page.classList.add("active");
    // scrollTop written while display:none is a no-op; land at the bottom
    // now that the page is actually visible.
    for (const scroller of [timelineEl.parentElement, loglinesEl]) {
      if (page.contains(scroller)) scroller.scrollTop = scroller.scrollHeight;
    }
  };

  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      activateTab(next);
    });
  });

  // ------------------------------------------------------------ live tab

  panicBtn.addEventListener("click", () => {
    send("panel.set", { panic_mute: !panicked });
  });

  const speakBoxes = new Map();

  const renderSpeak = (speak) => {
    // Update in place once built: a rebuild on every panel.state echo would
    // drop keyboard focus mid-click and flicker the matrix.
    for (const [key, value] of Object.entries(speak ?? {})) {
      const existing = speakBoxes.get(key);
      if (existing) {
        existing.checked = Boolean(value);
        continue;
      }
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
      speakBoxes.set(key, box);
    }
  };

  injectForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = injectInput.value.trim();
    if (!text) return;
    if (send("console.line", { text })) {
      injectInput.value = "";
    } else {
      // Disconnected: keep the text instead of silently eating it.
      feedEntry({ kind: "system", text: "连接断开，这条没发出去" });
    }
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

  // Which badge a frozen row wears; live rows get an editor instead.
  const RELOAD_BADGE = { reconnect: "重连生效", engine: "重启引擎", restart: "重启生效" };
  const CONFIG_NOTE = "亮着的控件直播中能改，只管本场；带徽章的行要到标注的时机才生效。";

  let configReloadTimer = null;
  let configRetryTimer = null;
  let configSeq = 0; // only the newest fetch may paint
  let advancedOpen = false; // the fold's state survives a rebuild
  let configNoteEl = null;
  // The live editors and read-only value spans, by path: a refresh updates
  // these in place instead of rebuilding the tab, which is what keeps focus,
  // an open dropdown and the advanced fold alive through it.
  const configControls = new Map();
  const configValues = new Map();

  const setConfigNote = (text, isError = false) => {
    if (!configNoteEl) return;
    configNoteEl.textContent = text;
    configNoteEl.classList.toggle("cfg-note-err", isError);
  };

  const setControlValue = (ctrl, value) => {
    // Remember what the server last said: the snap-back on a failed send has
    // to restore that, not whatever the user just clicked.
    ctrl.serverValue = value;
    if (ctrl.type === "checkbox") ctrl.checked = Boolean(value);
    else ctrl.value = String(value);
  };

  const sendEdit = (path, value, ctrl) => {
    if (!send("panel.set", { config: { path, value } })) {
      // Nothing reached the server, so the control must not keep showing the
      // new value — and the notice belongs on THIS tab, not only in the chat
      // feed the user cannot see from here.
      setControlValue(ctrl, ctrl.serverValue);
      setConfigNote("连接断开，这条修改没发出去", true);
      feedEntry({ kind: "system", text: "连接断开，这条修改没发出去" });
      return;
    }
    // The server acks into the feed; pull the canonical values shortly after
    // so a rejected edit visibly snaps back.
    clearTimeout(configReloadTimer);
    configReloadTimer = setTimeout(() => loadConfig(true), 700);
  };

  const editorFor = (row) => {
    if (row.kind === "bool") {
      const box = el("input");
      box.type = "checkbox";
      box.addEventListener("change", () => sendEdit(row.path, box.checked, box));
      return box;
    }
    if (row.kind === "select") {
      const sel = el("select");
      for (const choice of row.choices ?? []) {
        const opt = el("option", "", choice);
        opt.value = choice;
        sel.appendChild(opt);
      }
      sel.addEventListener("change", () => sendEdit(row.path, sel.value, sel));
      return sel;
    }
    const input = el("input");
    input.type = row.kind === "number" ? "number" : "text";
    if (row.kind === "number") {
      if (row.min !== null && row.min !== undefined) input.min = String(row.min);
      if (row.max !== null && row.max !== undefined) input.max = String(row.max);
      input.step = "any";
    }
    input.addEventListener("change", () => {
      if (row.kind !== "number") {
        sendEdit(row.path, input.value, input);
        return;
      }
      // Number("") is 0, and 0 passes a ge=0 bound — so a field cleared to be
      // retyped would silently commit zero. An empty box is not an edit.
      if (input.value.trim() === "") {
        setControlValue(input, input.serverValue);
        setConfigNote("数字不能留空，已还原", true);
        return;
      }
      sendEdit(row.path, Number(input.value), input);
    });
    return input;
  };

  // Push fresh values into the existing DOM. Returns false when the row set
  // changed shape (a different process, a new field) and only a rebuild will do.
  const updateInPlace = (rows) => {
    if (!configControls.size) return false;
    const editable = rows.filter((row) => row.editable && row.value !== null);
    if (editable.length !== configControls.size) return false;
    if (!editable.every((row) => configControls.has(row.path))) return false;
    for (const row of editable) {
      const ctrl = configControls.get(row.path);
      // Never yank a value out from under the cursor mid-edit; still record
      // what the server holds so a later failed send snaps back correctly.
      if (document.activeElement === ctrl) ctrl.serverValue = row.value;
      else setControlValue(ctrl, row.value);
    }
    for (const row of rows) {
      const span = configValues.get(row.path);
      if (span) span.textContent = `${row.value}${row.unit ? ` ${row.unit}` : ""}`;
    }
    return true;
  };

  // Speak switches have two editors (the live matrix and a config row); the
  // pushed panel.state keeps the config one honest without a fetch.
  const applySpeakToConfig = (speak) => {
    for (const [key, value] of Object.entries(speak ?? {})) {
      const ctrl = configControls.get(`interaction.speak.${key}`);
      if (ctrl && document.activeElement !== ctrl) setControlValue(ctrl, value);
    }
  };

  async function loadConfig(force = false) {
    if (configLoaded && !force) return;
    configLoaded = true;
    const seq = ++configSeq;
    const listEl = document.getElementById("config-list");
    let rows;
    try {
      rows = await (await fetch("config", { signal: AbortSignal.timeout(4000) })).json();
    } catch {
      if (seq !== configSeq) return; // a newer attempt owns the tab now
      configLoaded = false;
      if (configControls.size) {
        // Editors are already on screen: one flaky fetch must not wipe them.
        setConfigNote("配置接口暂时拿不到，显示的是上次读到的值", true);
        return;
      }
      listEl.textContent = "";
      configNoteEl = null;
      listEl.appendChild(el("p", "empty", "配置接口暂时拿不到，稍后自动重试"));
      clearTimeout(configRetryTimer);
      configRetryTimer = setTimeout(() => loadConfig(true), 5000);
      return;
    }
    // A slow earlier fetch resolving last would repaint pre-edit values over a
    // newer snapshot — the edit would appear to revert itself.
    if (seq !== configSeq) return;
    if (updateInPlace(rows)) {
      setConfigNote(CONFIG_NOTE);
      return;
    }
    listEl.textContent = "";
    configControls.clear();
    configValues.clear();
    configNoteEl = el("p", "cfg-note", CONFIG_NOTE);
    listEl.appendChild(configNoteEl);
    const advanced = el("details");
    advanced.open = advancedOpen;
    advanced.addEventListener("toggle", () => {
      advancedOpen = advanced.open;
    });
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
      const line = el("div", "cfg-row" + (row.editable ? " editable" : ""));
      line.dataset.path = row.path;
      line.appendChild(el("span", "cfg-label", row.label));
      if (row.editable) {
        const ctrl = editorFor(row);
        ctrl.classList.add("cfg-edit");
        setControlValue(ctrl, row.value);
        configControls.set(row.path, ctrl);
        if (row.unit) {
          const wrap = el("span", "cfg-editwrap");
          wrap.appendChild(ctrl);
          wrap.appendChild(el("span", "cfg-unit", row.unit));
          line.appendChild(wrap);
        } else {
          line.appendChild(ctrl);
        }
      } else {
        const unit = row.unit ? ` ${row.unit}` : "";
        const span = el("span", "cfg-value", `${row.value}${unit}`);
        configValues.set(row.path, span);
        line.appendChild(span);
        const badge = RELOAD_BADGE[row.reload];
        if (badge) line.appendChild(el("span", "cfg-badge", badge));
      }
      host.appendChild(line);
      if (row.hint) host.appendChild(el("p", "cfg-hint", row.hint));
    }
    listEl.appendChild(advanced);
  }

  // ------------------------------------------------------------ frames in

  if (panelOnly) {
    // The shell's second window has no open() to trigger the first load.
    startHealth();
    loadConfig();
  }

  return {
    handleFrame(event, data) {
      if (event === "event.feed") feedEntry(data);
      else if (event === "log.line") logEntry(data.line ?? "");
      else if (event === "panel.state") {
        panicked = Boolean(data.panicked);
        panicBtn.dataset.panicked = String(panicked);
        panicBtn.setAttribute("aria-pressed", String(panicked));
        panicBtn.textContent = panicked ? "恢复说话" : "紧急闭麦";
        renderSpeak(data.speak);
        applySpeakToConfig(data.speak);
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
    reset() {
      // Reconnect path: the server replays its rings into a fresh attach; a
      // panel keeping the old rows would show the history twice.
      timelineEl.textContent = "";
      timelineEl.appendChild(el("p", "empty", "还没有对话"));
      loglinesEl.textContent = "";
      // The config values came from the session that just ended — a restarted
      // dev-talk is back on the toml's values, so refetch instead of trusting
      // what is on screen.
      configLoaded = false;
      if (isOpen) loadConfig(true);
    },
  };
}
