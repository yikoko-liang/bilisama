// The control centre: system page (mic check, devices, speak matrix, room,
// reply strategy, manual mock), chat timeline with reply references, the
// assistant page (stage 10), logs, and the demoted advanced page (health +
// full config). It consumes event.feed / log.line / panel.state frames routed
// from main.js and pulls /health and /config over plain fetch — health only
// while someone is actually looking.

import { VISUAL_LABEL } from "./presentation.js";
import { unsupportedRenderer } from "./renderer.js";

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

// Room-feed rows the system page renders; everything else in event.feed
// belongs to the chat timeline or the log.
const LIVE_KINDS = new Set([
  "danmaku", "gift", "super_chat", "guard_buy", "vip_enter",
  "entry", "follow", "like", "share", "room_state",
]);
const LIVE_LABEL = {
  danmaku: "弹幕", gift: "礼物", super_chat: "SC", guard_buy: "上舰",
  vip_enter: "VIP进房", entry: "进房", follow: "关注", like: "点赞",
  share: "分享", room_state: "房间状态",
};
const GUARD_ZH = { captain: "舰长", admiral: "提督", governor: "总督" };
const SOURCE_ZH = {
  danmaku: "弹幕", super_chat: "SC", gift: "礼物", guard_buy: "上舰",
  vip_enter: "VIP进房", entry: "进房", proactive: "主动话题", voice: "语音",
};
const ROOM_EVENT_CAP = 100;

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
  const audioOwnerEl = document.getElementById("audio-owner");
  const audioInEl = document.getElementById("audio-in");
  const audioOutEl = document.getElementById("audio-out");
  const audioLevelEl = document.getElementById("audio-level");
  const audioTestEl = document.getElementById("audio-test");
  // No reference to the audio module here on purpose: inside the shell it
  // lives in the other window, and a panel that could sometimes call it
  // directly would work in a browser tab and quietly do nothing in the shell.
  // Everything goes over the wire, which is the same path in both.
  let ask = null;
  let levelTimer = null;
  let audioOwner = null; // whichever window the server says holds the devices
  // Local knowledge, and the only piece of it here: no audio.owner broadcast
  // can say 「this window asked for the microphone and was refused」, so once
  // heard it has to outlive the broadcasts that follow.
  let localMicError = null;
  const timelineEl = document.getElementById("timeline");
  const loglinesEl = document.getElementById("loglines");
  const logsTab = document.getElementById("tab-btn-logs");
  const levelSel = document.getElementById("log-level");
  const pauseBtn = document.getElementById("log-pause");
  const logPathEl = document.getElementById("log-path");
  const logRevealEl = document.getElementById("log-reveal");
  logRevealEl?.addEventListener("click", () => window.bilisamaShell?.revealLog?.());
  const injectForm = document.getElementById("inject");
  const injectInput = document.getElementById("inject-input");
  // System page.
  const inputToggle = document.getElementById("audio-input-enabled");
  const outputToggle = document.getElementById("audio-output-enabled");
  const signalMeter = document.getElementById("audio-signal-meter");
  const signalValue = document.getElementById("audio-signal-value");
  const noiseSlider = document.getElementById("noise-sensitivity");
  const noiseValue = document.getElementById("noise-value");
  const roomIdInput = document.getElementById("room-id");
  const roomConnectBtn = document.getElementById("room-connect");
  const roomDisconnectBtn = document.getElementById("room-disconnect");
  const streamerNameInput = document.getElementById("streamer-name");
  const streamerNameSave = document.getElementById("streamer-name-save");
  const streamIntroInput = document.getElementById("stream-intro");
  const roomInfoSave = document.getElementById("room-info-save");
  const roomInfoHint = document.getElementById("room-info-hint");
  const roomStatusEl = document.getElementById("room-status");
  const roomEventsEl = document.getElementById("room-events");
  const chattinessSel = document.getElementById("chattiness");
  const replyLengthSel = document.getElementById("reply-length");
  const giftMediumInput = document.getElementById("gift-medium");
  const giftHighInput = document.getElementById("gift-high");
  const entryGroupBoxes = [...document.querySelectorAll("[data-entry-group]")];

  const panelOnly = document.body.classList.contains("panel-only");
  let isOpen = panelOnly;
  let panicked = false;
  let onPanelState = null;
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
        // Every field, not the first few: the selector reports seven and its
        // last three are breaker_open / breaker_reason / combos_suppressed
        // (ingest/bilibili/selector.py:181-189) — the ones a streamer needs
        // mid-stream. A cut-off card hid the breaker exactly when it tripped.
        kv.textContent = Object.entries(data ?? {})
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
    // The meter belongs to the visible panel exactly like health does; see
    // startLevelMeter for what it costs while nobody is looking.
    startLevelMeter();
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
    stopLevelMeter();
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
    if (tab === logsTab) tab.removeAttribute("data-alert"); // read; stop nagging
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
    // as_live: the mock box exists to exercise the REAL room funnel — window,
    // budget, coalescing — not the console's direct lane.
    if (send("console.line", { text, as_live: true })) {
      injectInput.value = "";
    } else {
      // Disconnected: keep the text instead of silently eating it.
      feedEntry({ kind: "system", text: "连接断开，这条没发出去" });
    }
  });

  // ------------------------------------------------------------ system page

  const sendConfig = (path, value) => {
    if (!send("panel.set", { config: { path, value } })) {
      feedEntry({ kind: "system", text: "连接断开，这条修改没发出去" });
    }
  };

  inputToggle?.addEventListener("change", () => {
    send("panel.set", { audio: { input_enabled: inputToggle.checked } });
  });
  outputToggle?.addEventListener("change", () => {
    send("panel.set", { audio: { output_enabled: outputToggle.checked } });
  });
  noiseSlider?.addEventListener("input", () => {
    if (noiseValue) noiseValue.textContent = noiseSlider.value;
  });
  // change, not input: committing every drag tick would spam the edit channel.
  noiseSlider?.addEventListener("change", () => {
    send("panel.set", { audio: { noise_sensitivity: Number(noiseSlider.value) } });
  });

  chattinessSel?.addEventListener("change", () => {
    sendConfig("interaction.chattiness", chattinessSel.value);
  });
  replyLengthSel?.addEventListener("change", () => {
    sendConfig("interaction.reply_length", replyLengthSel.value);
  });
  const giftEdit = (input, path) => {
    input?.addEventListener("change", () => {
      const value = Number(input.value);
      if (!Number.isFinite(value) || value < 1) return;
      sendConfig(path, Math.round(value));
    });
  };
  giftEdit(giftMediumInput, "interaction.gift_battery_medium");
  giftEdit(giftHighInput, "interaction.gift_battery_high");
  for (const box of entryGroupBoxes) {
    box.addEventListener("change", () => {
      sendConfig(`interaction.entry_welcome.${box.dataset.entryGroup}`, box.checked);
    });
  }

  // ---- room card ----

  let roomPending = false;
  let lastRoomState = null;

  const setRoomPending = (pending, hint) => {
    roomPending = pending;
    if (roomConnectBtn) roomConnectBtn.disabled = pending;
    if (roomInfoHint && hint) roomInfoHint.textContent = hint;
  };

  roomConnectBtn?.addEventListener("click", () => {
    const id = Number((roomIdInput?.value ?? "").trim());
    if (!Number.isInteger(id) || id <= 0) {
      if (roomStatusEl) roomStatusEl.textContent = "直播间号要是正整数";
      return;
    }
    setRoomPending(true, "正在连接…");
    if (roomStatusEl) roomStatusEl.textContent = `正在连接直播间 ${id}…`;
    send("panel.set", { room: { action: "connect", room_id: id } });
  });
  roomDisconnectBtn?.addEventListener("click", () => {
    setRoomPending(true, "正在断开…");
    send("panel.set", { room: { action: "disconnect" } });
  });

  const refreshRoomInfoDirty = () => {
    const server = lastRoomState ?? {};
    if (streamerNameSave && streamerNameInput) {
      streamerNameSave.disabled =
        streamerNameInput.value.trim() === String(server.streamer_name ?? "");
    }
    if (roomInfoSave && streamIntroInput) {
      roomInfoSave.disabled = streamIntroInput.value === String(server.stream_intro ?? "");
    }
  };
  streamerNameInput?.addEventListener("input", refreshRoomInfoDirty);
  streamIntroInput?.addEventListener("input", refreshRoomInfoDirty);
  streamerNameSave?.addEventListener("click", () => {
    sendConfig("persona.streamer_name", streamerNameInput?.value.trim() ?? "");
  });
  roomInfoSave?.addEventListener("click", () => {
    sendConfig("room.stream_intro", streamIntroInput?.value ?? "");
  });

  const identityBits = (data) => {
    const bits = [];
    if (data.is_anchor) bits.push("主播本人");
    const guard = GUARD_ZH[data.guard_level];
    if (guard) bits.push(guard);
    if (data.is_admin) bits.push("房管");
    const medal = data.medal;
    if (medal && medal.name) {
      bits.push(`${medal.this_room ? "本房" : "外房"}粉丝牌${medal.name}·Lv${medal.level}`);
    }
    if (data.user_level) bits.push(`用户Lv${data.user_level}`);
    return bits.join(" · ");
  };

  const roomEvent = (data) => {
    if (!roomEventsEl) return;
    roomEventsEl.querySelector(".empty")?.remove();
    const row = el("div", `room-event ${data.kind}`);
    row.appendChild(el("span", "when", new Date().toTimeString().slice(0, 8)));
    row.appendChild(el("span", "pill", LIVE_LABEL[data.kind] ?? data.kind));
    let body = data.name ?? "?";
    if (data.gift) body += `：${data.gift.name} ×${data.gift.num}`;
    else if (data.text) body += `：${data.text}`;
    const identity = identityBits(data);
    if (identity) body += `（${identity}）`;
    if (data.mock) body += "〔Mock〕";
    row.appendChild(el("span", "body", body));
    roomEventsEl.appendChild(row);
    while (roomEventsEl.children.length > ROOM_EVENT_CAP) roomEventsEl.firstChild.remove();
    roomEventsEl.scrollTop = roomEventsEl.scrollHeight;
  };

  const applySystemState = (state) => {
    const audio = state.audio ?? {};
    if (inputToggle && document.activeElement !== inputToggle) {
      inputToggle.checked = Boolean(audio.input_enabled);
    }
    if (outputToggle && document.activeElement !== outputToggle) {
      outputToggle.checked = Boolean(audio.output_enabled);
    }
    if (noiseSlider && document.activeElement !== noiseSlider) {
      noiseSlider.value = String(audio.noise_sensitivity ?? 50);
      if (noiseValue) noiseValue.textContent = noiseSlider.value;
    }
    const interaction = state.interaction ?? {};
    if (chattinessSel && document.activeElement !== chattinessSel) {
      chattinessSel.value = interaction.chattiness ?? "medium";
    }
    if (replyLengthSel && document.activeElement !== replyLengthSel) {
      replyLengthSel.value = interaction.reply_length ?? "low";
    }
    if (giftMediumInput && document.activeElement !== giftMediumInput) {
      giftMediumInput.value = String(interaction.gift_battery_medium ?? "");
    }
    if (giftHighInput && document.activeElement !== giftHighInput) {
      giftHighInput.value = String(interaction.gift_battery_high ?? "");
    }
    const groups = interaction.entry_welcome ?? {};
    for (const box of entryGroupBoxes) {
      if (document.activeElement !== box) {
        box.checked = Boolean(groups[box.dataset.entryGroup]);
      }
    }
    const room = state.room ?? {};
    const previousRoom = lastRoomState;
    lastRoomState = room;
    // Never overwrite what someone is typing; refresh the dirty flags either way.
    if (roomIdInput && document.activeElement !== roomIdInput && !roomPending) {
      const active = Number(room.active_room_id ?? 0);
      if (active > 0) roomIdInput.value = String(active);
    }
    if (streamerNameInput && document.activeElement !== streamerNameInput) {
      streamerNameInput.value = String(room.streamer_name ?? "");
    }
    if (streamIntroInput && document.activeElement !== streamIntroInput) {
      streamIntroInput.value = String(room.stream_intro ?? "");
    }
    refreshRoomInfoDirty();
    if (roomStatusEl) {
      if (room.connected) {
        roomStatusEl.textContent = `已连接直播间 ${room.active_room_id}`;
        roomStatusEl.dataset.state = "on";
      } else if (room.error) {
        roomStatusEl.textContent = `连接失败：${room.error}`;
        roomStatusEl.dataset.state = "err";
      } else {
        roomStatusEl.textContent = "尚未连接直播间";
        roomStatusEl.dataset.state = "off";
      }
    }
    if (roomDisconnectBtn) roomDisconnectBtn.disabled = !room.connected;
    if (roomPending && (room.connected || room.error)) setRoomPending(false, "");
    // A new room means a new stream of events; the old rows are last room's.
    if (
      previousRoom &&
      roomEventsEl &&
      Number(previousRoom.active_room_id ?? 0) !== Number(room.active_room_id ?? 0)
    ) {
      roomEventsEl.textContent = "";
      roomEventsEl.appendChild(el("p", "empty", "等待新直播间事件…"));
    }
  };

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
      const copy = el("span", "reply-copy");
      const reference = data.reference;
      if (reference && data.source && data.source !== "voice") {
        // 「这句在接谁」——听录音的人对得上，看面板的人也对得上。
        const label = SOURCE_ZH[data.source] ?? data.source;
        const refText = reference.text
          ? `${reference.name ?? "?"}：${reference.text}`
          : reference.name ?? label;
        copy.appendChild(el("span", "reply-reference", `引用 ${label} · ${refText}`));
      }
      copy.appendChild(el("span", "", `${data.text || "（无文本）"}${status}`));
      entry.appendChild(copy);
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

  // A healthy session logs almost nothing. Only 15 call sites in the whole of
  // src log at info, and the per-turn detail a streamer actually watches —
  // dispatch verdicts, context pushes, barge-ins — goes to the terminal through
  // print() and never enters the logging stream at all. Measured on three real
  // sessions: one JSON line each, against 14 to 99 printed ones. So an empty
  // log pane is the normal state, and a blank box reads as a broken feature.
  const logEmpty = el(
    "p",
    "empty",
    "这一场还没有日志。这里只在出问题时才有内容——调度结论和打断在「对话」页看。",
  );
  loglinesEl.appendChild(logEmpty);

  const pushLog = (node, rank) => {
    logEmpty.remove();
    node.dataset.rank = String(rank);
    node.hidden = rank < (LOG_RANK[levelSel.value] ?? 1);
    loglinesEl.appendChild(node);
    while (loglinesEl.children.length > LOG_CAP) loglinesEl.firstChild.remove();
    if (!logPaused) loglinesEl.scrollTop = loglinesEl.scrollHeight;
  };

  const logEntry = (line) => {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      record = { level: "info", event: line };
    }
    const node = el("div", `logline ${record.level ?? "info"}`);
    const rest = Object.entries(record)
      .filter(([k]) => !["ts", "level", "event", "logger"].includes(k))
      .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
      .join(" ");
    node.textContent = `${clock(record.ts ?? "")} ${record.level ?? ""} ${record.event ?? ""} ${rest}`;
    pushLog(node, LOG_RANK[record.level] ?? 1);
  };

  // Lines this page writes about itself. The server's log stream cannot carry
  // them: a skin that failed to load, or a renderer this build has no
  // implementation for, is knowledge that exists only in the browser. They
  // used to go to console.warn, which inside the shell means nowhere at all —
  // there are no devtools on a frameless always-on-top window, so a degrade
  // read as 「换了皮肤怎么还是豆腐」 (§15.12 asked for a line in the log area).
  const noticed = new Set(); // hello repeats on every attach; the fact does not
  const notice = (text) => {
    if (!text || noticed.has(text)) return;
    noticed.add(text);
    const node = el("div", "logline warning");
    // Local wall clock, matching the server lines' HH:MM:SS beside it —
    // toISOString would print UTC and read as an hours-old line.
    node.textContent = `${new Date().toTimeString().slice(0, 8)} warning ${text}`;
    pushLog(node, LOG_RANK.warning);
    // The panel does not open on the log tab, and a line nobody is pointed at
    // is barely better than the console it came from.
    logsTab?.setAttribute("data-alert", "1");
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

  // ------------------------------------------------------------ audio

  // Device names arrive empty until the microphone is granted, so the panel
  // asks after a claim rather than on load — a list of 「未命名设备」 helps
  // nobody. It asks over the wire in every case: inside the shell the devices
  // are in the pet window and the settings are here, and a direct call would
  // work in a browser tab and silently do nothing in the shell. Which is
  // exactly how this shipped the first time: 「读取中…」 forever.
  function request(what, extra = {}) {
    ask?.({ what, ...extra });
  }

  function renderDevices(devices) {
    if (!audioInEl) return;
    const fill = (select, kind) => {
      // The placeholder carries value="" so this survives the first render:
      // without it `previous` was the literal 「读取中…」, nothing matched on
      // the way back, and selectedIndex fell to -1 — both dropdowns came up
      // blank instead of 「跟随系统」, which is what sent people clicking at
      // them in the first place.
      const previous = select.value;
      select.replaceChildren();
      const auto = el("option", null, "跟随系统");
      auto.value = "";
      select.append(auto);
      for (const device of devices.filter((one) => one.kind === kind)) {
        const option = el("option", null, device.label);
        option.value = device.id;
        select.append(option);
      }
      select.value = previous; // survives a re-enumeration after a hot-plug
      // Unless what was selected is gone — unplugged, or renamed by the driver.
      // The value setter cancels every option when none matches, which lands
      // selectedIndex on -1 and shows a blank box: no 「跟随系统」, no name of
      // whatever is actually recording now. Falling back is what the frame
      // handler already promises for this case ("put the dropdown back on what
      // is really playing", panel.js's audio.devices arm).
      if (select.selectedIndex < 0) select.value = "";
    };
    fill(audioInEl, "audioinput");
    fill(audioOutEl, "audiooutput");
  }

  audioInEl?.addEventListener("change", () => request("use_input", { id: audioInEl.value }));
  audioOutEl?.addEventListener("change", () => request("use_output", { id: audioOutEl.value }));
  audioTestEl?.addEventListener("click", () => request("test"));

  function startLevelMeter() {
    if (levelTimer || !audioLevelEl) return;
    // Only while someone can see the bar. The trigger is the audio.owner
    // broadcast, which every client gets, so without this every window polls —
    // including the shell's pet window, whose drawer never opens at all (the
    // corner spawns a separate #panel window). And every ask fans out to every
    // client twice — once as the command, once as the answer (server.py's
    // audio relay) — so the cost multiplies by windows rather than adding up.
    if (!isOpen) return;
    // Nothing to read: no holder, or a holder whose microphone this window was
    // refused — a bar frozen at 0% reads as 「she is silent」 rather than
    // 「there is no meter」.
    if (!audioOwner || localMicError) return;
    // 10 Hz over the wire: fast enough to read as live, and small enough that
    // it costs nothing next to the voice-state poll already running.
    levelTimer = setInterval(() => request("level"), 100);
  }

  function stopLevelMeter() {
    clearInterval(levelTimer);
    levelTimer = null;
    if (audioLevelEl) audioLevelEl.style.width = "0%";
  }

  // A promise-shaped confirm dialog, shared by the assistant page and any
  // future destructive action. Esc is captured before the panel's own
  // close-on-Escape handler.
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmMessage = document.getElementById("confirm-message");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmAccept = document.getElementById("confirm-accept");
  const confirmCancel = document.getElementById("confirm-cancel");
  const confirmAction = ({ title, message, accept = "确认" }) =>
    new Promise((resolve) => {
      if (!confirmDialog) {
        resolve(true);
        return;
      }
      confirmTitle.textContent = title ?? "确认操作";
      confirmMessage.textContent = message ?? "";
      confirmAccept.textContent = accept;
      confirmDialog.hidden = false;
      const done = (answer) => {
        confirmDialog.hidden = true;
        confirmAccept.removeEventListener("click", yes);
        confirmCancel.removeEventListener("click", no);
        document.removeEventListener("keydown", esc, true);
        resolve(answer);
      };
      const yes = () => done(true);
      const no = () => done(false);
      const esc = (e) => {
        if (e.key !== "Escape") return;
        e.stopImmediatePropagation();
        no();
      };
      confirmAccept.addEventListener("click", yes);
      confirmCancel.addEventListener("click", no);
      document.addEventListener("keydown", esc, true);
    });

  return {
    /** How to reach whichever window holds the devices. */
    setAsk(fn) {
      ask = fn;
    },
    /** The main page mirrors pause/audio state onto the pet controls. */
    setOnPanelState(fn) {
      onPanelState = fn;
    },
    confirmAction,
    setAudioOwner(owner, error) {
      if (!audioOwnerEl) return;
      if (error) {
        localMicError = error;
        audioOwnerEl.textContent = `拿不到麦克风：${error}`;
        stopLevelMeter();
        return;
      }
      audioOwner = owner ?? null;
      if (!owner) {
        // Devices free again, so the last refusal stops being news: a window
        // that stood aside re-claims from here (audio.js's retry), and whether
        // the microphone comes back is answered by THAT attempt.
        localMicError = null;
        audioOwnerEl.textContent = "还没有窗口接管声音，走的是本机播放。";
        stopLevelMeter();
        return;
      }
      if (localMicError) {
        // audio.owner is sticky and replays into every attach, so before this
        // the first broadcast after a refusal painted over 「拿不到麦克风」 with
        // 「已接管」 and left it there: the streamer was told the microphone was
        // live while not one word reached the server.
        audioOwnerEl.textContent = `已接管，但本窗口拿不到麦克风：${localMicError}`;
      } else {
        // What we can honestly say: the browser accepted the request. Whether
        // the echo is actually gone depends on where the sound comes out — the
        // canceller only subtracts Chromium's own playback, so OBS monitoring,
        // a game or background music go straight into the microphone no matter
        // what this line says. Claiming 「回声消除已开」 outright was a promise
        // this window has no way to keep.
        audioOwnerEl.textContent = "麦克风和扬声器已接管（回声消除已请求）。";
      }
      request("devices");
      startLevelMeter();
    },
    /** This window stopped being the one with bad news about a microphone.
     *
     * Either it just got one, or something stronger took the devices. Both
     * mean the last refusal has stopped describing whoever is holding them
     * now: a window displaced by a healthy one used to go on saying
     * 「本窗口拿不到麦克风」 until every window let go, which reads as a fault
     * in the shell that is actually capturing perfectly well.
     */
    forgetLocalMicTrouble() {
      if (!localMicError) return;
      localMicError = null;
      if (audioOwner) this.setAudioOwner(audioOwner);
    },
    handleFrame(event, data) {
      if (event === "audio.owner") {
        // Broadcast to everyone. A panel window never holds the devices itself
        // — inside the shell they are in the pet window — so it reports on the
        // holder rather than on itself, and drives it over the wire.
        this.setAudioOwner(data.owner ?? null);
        return;
      }
      if (event === "audio.devices") {
        renderDevices(data.devices ?? []);
        if (data.error) {
          // Usually the selected device was unplugged. Say so, and let the
          // re-rendered list put the dropdown back on what is really playing.
          audioOwnerEl.textContent = `切换设备没成功：${data.error}`;
        } else if (data.moved === false) {
          audioOwnerEl.textContent = "这个浏览器不支持切换扬声器，用系统默认。";
          audioOutEl.value = "";
        }
        return;
      }
      if (event === "audio.level") {
        if (data.source === "uplink") {
          // The server-side meter: what the provider actually hears, 0-100,
          // after the noise gate's metering. The device row below keeps its
          // own ask/report meter — that one must stay alive when the INPUT
          // switch is off, this one goes quiet with it.
          const level = Math.max(0, Math.min(100, Number(data.level) || 0));
          if (signalMeter) signalMeter.style.width = `${level}%`;
          if (signalValue) signalValue.textContent = String(level);
          return;
        }
        if (audioLevelEl) {
          const level = Math.min(1, Number(data.level) || 0);
          audioLevelEl.style.width = `${Math.round(level * 100)}%`;
        }
        return;
      }
      if (event === "event.feed") {
        if (LIVE_KINDS.has(data.kind)) {
          // Room events land on the system page's flow; danmaku/SC/gift also
          // read well in the chat timeline, where they sit next to the reply
          // that answers them.
          roomEvent(data);
          if (!["danmaku", "gift", "super_chat"].includes(data.kind)) return;
          if (data.kind === "super_chat") feedEntry({ ...data, kind: "sc" });
          else feedEntry(data);
          return;
        }
        feedEntry(data);
      } else if (event === "log.line") logEntry(data.line ?? "");
      else if (event === "panel.state") {
        panicked = Boolean(data.panicked);
        panicBtn.dataset.panicked = String(panicked);
        panicBtn.setAttribute("aria-pressed", String(panicked));
        panicBtn.textContent = panicked ? "恢复说话" : "紧急叫停";
        renderSpeak(data.speak);
        applySpeakToConfig(data.speak);
        applySystemState(data);
        onPanelState?.(data);
      }
    },
    /** One line about this page, in the place the streamer can read. */
    notice,
    setHello(data) {
      nameEl.textContent = data.persona?.name ?? "BiliSama";
      // Driven from hello rather than from the mount, because the panel window
      // inside the shell mounts no pet at all and would otherwise never hear
      // that the configured renderer did not take.
      notice(unsupportedRenderer(data.avatar));
      // Where the record outlives this window. The pane keeps 500 lines and
      // dies with the tab; 「昨天那次她为什么没说话」 is only answerable from
      // the file, so the path is on screen rather than in a doc somewhere.
      if (data.log_path && logPathEl) {
        logPathEl.textContent = `日志文件：${data.log_path}`;
        logPathEl.hidden = false;
        // Only the shell can open a directory. A button that does nothing in a
        // browser tab is worse than no button, so it appears where it works.
        if (logRevealEl && window.bilisamaShell?.revealLog) logRevealEl.hidden = false;
      }
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
      // The local lines went with them, so they are news again — otherwise a
      // reconnect silently retires the one notice explaining a dead skin.
      noticed.clear();
      // The config values came from the session that just ended — a restarted
      // dev-talk is back on the toml's values, so refetch instead of trusting
      // what is on screen.
      configLoaded = false;
      if (isOpen) loadConfig(true);
    },
  };
}
