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

// Kinds with a switch but no speaking intent behind it (ledger #50): the
// matrix pins them off and grey instead of promising speech that never comes.
const NO_VOICE_SOURCES = new Set(["follow", "like", "share"]);

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

// One vocabulary for money across the room feed, the chat feed and the reply
// reference — live_event_payload is one shape, so its rendering is one too.
function giftBits(gift) {
  if (!gift) return "";
  let text = `${gift.name} ×${gift.num}`;
  if (gift.total_battery) text += `（${gift.total_battery} 电池）`;
  return text;
}
function amountBits(data) {
  if (data.gift) return giftBits(data.gift);
  return data.value_cny ? `¥${Math.round(data.value_cny)}` : "";
}
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
  const skinCardsEl = document.getElementById("skin-cards");
  const testSetSwitch = document.getElementById("test-set-switch");
  const testDescription = document.getElementById("test-description");
  const testCandidate = document.getElementById("test-candidate");
  const testCount = document.getElementById("test-count");
  const testStop = document.getElementById("test-stop");
  const testCases = document.getElementById("test-cases");

  const panelOnly = document.body.classList.contains("panel-only");
  let isOpen = panelOnly;
  let panicked = false;
  let onPanelState = null;
  let onOpenChange = null;
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
    onOpenChange?.(true);
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
    onOpenChange?.(false);
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
        if (!existing.disabled) existing.checked = Boolean(value);
        continue;
      }
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      if (NO_VOICE_SOURCES.has(key)) {
        // Ledger #50: these kinds have no speaking intent in the backend, so
        // a checkable box is a placebo. Grey and pinned off until they do.
        box.checked = false;
        box.disabled = true;
        label.title = "后端暂时没有这类语音，开关未生效";
      } else {
        box.checked = Boolean(value);
        box.addEventListener("change", () => {
          send("panel.set", { speak: { [key]: box.checked } });
        });
      }
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
  let roomPendingKind = ""; // "connect" | "disconnect" — decides which state settles it
  let lastRoomState = null;

  const setRoomPending = (pending, kind, hint) => {
    roomPending = pending;
    roomPendingKind = pending ? kind : "";
    if (roomConnectBtn) roomConnectBtn.disabled = pending;
    if (roomInfoHint && hint) roomInfoHint.textContent = hint;
  };

  roomConnectBtn?.addEventListener("click", () => {
    const id = Number((roomIdInput?.value ?? "").trim());
    if (!Number.isInteger(id) || id <= 0) {
      if (roomStatusEl) {
        roomStatusEl.textContent = "直播间号要是正整数";
        roomStatusEl.dataset.state = "err";
      }
      return;
    }
    setRoomPending(true, "connect", "正在连接…");
    if (roomStatusEl) roomStatusEl.textContent = `正在连接直播间 ${id}…`;
    send("panel.set", { room: { action: "connect", room_id: id } });
  });
  roomDisconnectBtn?.addEventListener("click", () => {
    roomDisconnectBtn.disabled = true; // one click, one disconnect
    setRoomPending(true, "disconnect", "正在断开…");
    if (roomStatusEl) roomStatusEl.textContent = "正在断开直播间事件流…";
    send("panel.set", { room: { action: "disconnect" } });
  });

  const roomInfoDefaultHint = roomInfoHint?.textContent ?? "";

  const refreshRoomInfoDirty = () => {
    const server = lastRoomState ?? {};
    const nameDirty =
      Boolean(streamerNameInput) &&
      streamerNameInput.value.trim() !== String(server.streamer_name ?? "");
    const introDirty =
      Boolean(streamIntroInput) && streamIntroInput.value !== String(server.stream_intro ?? "");
    if (streamerNameSave) streamerNameSave.disabled = !nameDirty;
    if (roomInfoSave) roomInfoSave.disabled = !introDirty;
    if (roomInfoHint && !roomPending) {
      roomInfoHint.textContent = nameDirty || introDirty ? "有未保存修改" : roomInfoDefaultHint;
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
    if (data.wealth_level) bits.push(`荣耀Lv${data.wealth_level}`);
    return bits.join(" · ");
  };

  const roomEvent = (data) => {
    if (!roomEventsEl) return;
    roomEventsEl.querySelector(".empty")?.remove();
    const row = el("div", `room-event ${data.kind}`);
    // The event's own clock when it has one: a replayed or delayed frame must
    // not claim it happened "now".
    const stamp = data.ts_ms ? new Date(data.ts_ms) : new Date();
    row.appendChild(el("span", "when", stamp.toTimeString().slice(0, 8)));
    row.appendChild(el("span", "pill", LIVE_LABEL[data.kind] ?? data.kind));
    let body = data.name ?? "?";
    if (data.gift) body += `：${giftBits(data.gift)}`;
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
    if (
      noiseSlider &&
      document.activeElement !== noiseSlider &&
      Number.isFinite(audio.noise_sensitivity)
    ) {
      noiseSlider.value = String(audio.noise_sensitivity);
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
    // Backfill protection: focus is not the whole story — a field the user
    // edited and clicked away from is still theirs until saved. Compare with
    // the PREVIOUS server value: matching it means untouched, overwrite away.
    // No force-overwrite flag: after a save lands, the server echoes the
    // typed value, the compare converges on its own, and a shared flag was
    // exactly how saving ONE field clobbered the other's unsaved edit.
    const prevRoom = previousRoom ?? {};
    if (streamerNameInput && document.activeElement !== streamerNameInput) {
      const untouched = streamerNameInput.value.trim() === String(prevRoom.streamer_name ?? "");
      if (untouched) streamerNameInput.value = String(room.streamer_name ?? "");
    }
    if (streamIntroInput && document.activeElement !== streamIntroInput) {
      const untouched = streamIntroInput.value === String(prevRoom.stream_intro ?? "");
      if (untouched) streamIntroInput.value = String(room.stream_intro ?? "");
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
    // A connect settles on connected-or-error; a clean disconnect settles on
    // NEITHER (connected=false, error="") — keying both on the same pair left
    // the pending latch stuck after every successful disconnect, with the
    // connect button dead until a reload.
    const settled =
      roomPendingKind === "disconnect" ? !room.connected || room.error : room.connected || room.error;
    if (roomPending && settled) {
      setRoomPending(false, "", "");
      // The dirty refresh above ran while pending still held the hint; run it
      // again or 「正在断开…」 stays painted until the next state frame.
      refreshRoomInfoDirty();
    }
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
        let refText = reference.text
          ? `${reference.name ?? "?"}：${reference.text}`
          : (reference.name ?? label);
        // The amount explains the queue-jump; the model never hears it, the
        // operator should. Gifts speak batteries (the panel unit), money ¥.
        const amount = amountBits(reference);
        if (amount) refText += `〔${amount}〕`;
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

  // ------------------------------------------------------------ assistants

  const assistantCards = document.getElementById("assistant-cards");
  const assistantEditor = document.getElementById("assistant-editor");
  const assistantTitle = document.getElementById("assistant-editor-title");
  const assistantIdentity = document.getElementById("assistant-identity");
  const assistantPersonality = document.getElementById("assistant-personality");
  const assistantSave = document.getElementById("assistant-save");
  let assistants = [];
  let shownAssistant = null; // which card the editor is showing
  let editorDirty = false;

  const refreshAssistantDirty = () => {
    const card = assistants.find((one) => one.id === shownAssistant);
    editorDirty =
      Boolean(card) &&
      (assistantIdentity.value !== card.identity ||
        assistantPersonality.value !== card.personality);
    if (assistantSave) assistantSave.disabled = !editorDirty;
  };
  assistantIdentity?.addEventListener("input", refreshAssistantDirty);
  assistantPersonality?.addEventListener("input", refreshAssistantDirty);

  const showAssistant = (card) => {
    shownAssistant = card.id;
    if (assistantEditor) assistantEditor.hidden = false;
    if (assistantTitle) assistantTitle.textContent = `${card.name} · 人设配置`;
    assistantIdentity.value = card.identity;
    assistantPersonality.value = card.personality;
    refreshAssistantDirty();
  };

  const renderAssistants = () => {
    if (!assistantCards) return;
    assistantCards.textContent = "";
    if (!assistants.length) {
      assistantCards.appendChild(el("p", "empty", "没有读到人设包"));
      return;
    }
    for (const card of assistants) {
      const node = el("button", "assistant-card" + (card.current ? " current" : ""));
      node.type = "button";
      const face = el("span", "assistant-face", card.name.slice(0, 1));
      // A deterministic hue per persona keeps the cards tellable apart
      // without shipping four portraits.
      let hash = 0;
      for (const ch of card.id) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
      face.style.background = `hsl(${hash} 55% 55%)`;
      node.appendChild(face);
      node.appendChild(el("span", "assistant-name", card.name));
      if (card.description) node.appendChild(el("span", "assistant-desc", card.description));
      node.appendChild(el("span", "assistant-state", card.current ? "当前人设" : "点击查看"));
      node.addEventListener("click", async () => {
        showAssistant(card);
        if (card.current) return;
        const ok = await confirmAction({
          title: `切换到「${card.name}」？`,
          message: "整套人设立即热更新：锚点、生长文件和提示词都换过去，不重连语音服务。",
          accept: "切换",
        });
        if (!ok) return;
        send("panel.set", { assistant: { action: "select", id: card.id } });
      });
      assistantCards.appendChild(node);
    }
  };

  // ---- the appearance half of the 「形象与声音」 card ----

  let appearanceState = null;

  const renderSkins = () => {
    if (!skinCardsEl || !appearanceState) return;
    const skins = appearanceState.skins ?? [];
    const currentId = appearanceState.avatar?.model_id || "tofu";
    skinCardsEl.textContent = "";
    if (!skins.length) {
      skinCardsEl.appendChild(el("p", "empty", "没有读到皮肤包"));
      return;
    }
    for (const skin of skins) {
      const node = el("button", "skin-card" + (skin.id === currentId ? " current" : ""));
      node.type = "button";
      const face = el("span", "assistant-face", skin.id.slice(0, 1).toUpperCase());
      let hash = 120;
      for (const ch of skin.id) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
      face.style.background = `hsl(${hash} 45% 50%)`;
      node.appendChild(face);
      node.appendChild(el("span", "assistant-name", skin.id === "tofu" ? "豆腐（内置）" : skin.id));
      node.appendChild(
        el("span", "assistant-state", skin.id === currentId ? "当前形象" : skin.source === "user" ? "导入包" : "内置"),
      );
      node.addEventListener("click", () => {
        if (skin.id === currentId) return;
        // Cheap and reversible — no confirm. Empty means the built-in, which
        // is how the config spells "tofu" since the v4 axis split.
        sendConfig("avatar.model_id", skin.id === "tofu" ? "" : skin.id);
      });
      skinCardsEl.appendChild(node);
    }
  };

  const applySkins = (appearance) => {
    if (!appearance || typeof appearance !== "object") return;
    appearanceState = appearance;
    renderSkins();
  };

  assistantSave?.addEventListener("click", async () => {
    const card = assistants.find((one) => one.id === shownAssistant);
    if (!card || !editorDirty) return;
    const ok = await confirmAction({
      title: `保存「${card.name}」的人设？`,
      message: card.current
        ? "保存后立即热更新当前会话；随包原稿不受影响。"
        : "保存进该人设的活副本；切换到它时生效。",
      accept: "保存",
    });
    if (!ok) return;
    if (assistantIdentity.value !== card.identity) {
      send("panel.set", {
        assistant: { action: "save", id: card.id, anchor: "identity", text: assistantIdentity.value },
      });
    }
    if (assistantPersonality.value !== card.personality) {
      send("panel.set", {
        assistant: {
          action: "save",
          id: card.id,
          anchor: "personality",
          text: assistantPersonality.value,
        },
      });
    }
  });

  const applyAssistants = (cards) => {
    if (!Array.isArray(cards)) return;
    assistants = cards;
    renderAssistants();
    const shown = assistants.find((one) => one.id === shownAssistant);
    if (!shown) {
      // The card the editor was showing is gone; fall back to the current one.
      const current = assistants.find((one) => one.current);
      if (current && assistantEditor && !assistantEditor.hidden) showAssistant(current);
      return;
    }
    if (assistantTitle) assistantTitle.textContent = `${shown.name} · 人设配置`;
    // A dirty editor is the user's text; only clean editors follow the server.
    if (!editorDirty) {
      assistantIdentity.value = shown.identity;
      assistantPersonality.value = shown.personality;
    }
    refreshAssistantDirty();
  };


  // ------------------------------------------------------------ test console

  let testSets = [];
  let activeTestSet = "functional";
  let activeCandidate = "";
  let testState = { status: "idle", case_id: "" };
  // Local only, on purpose: the pass/fail button is the human's scratchpad
  // for THIS sitting, not a record the backend keeps.
  const testJudgments = new Map();

  const isTestRunning = () => ["running", "event"].includes(testState.status);

  const applyTestState = () => {
    if (!testCases) return;
    const running = isTestRunning();
    if (testStop) {
      testStop.hidden = false;
      testStop.disabled = !running;
    }
    for (const card of testCases.querySelectorAll(".test-card")) {
      const selected = card.dataset.caseId === testState.case_id;
      card.classList.toggle("running", selected && running);
      card.classList.toggle("failed", selected && testState.status === "failed");
      const run = card.querySelector(".test-run");
      run.disabled = running;
      run.textContent = selected && running ? "运行中…" : "运行";
      const status = card.querySelector(".test-status");
      if (!selected) {
        status.textContent = "";
        continue;
      }
      if (testState.status === "running") status.textContent = "已启动，等待第一条事件";
      else if (testState.status === "event") {
        status.textContent = `已注入 ${testState.index ?? 0}/${testState.total ?? 0}：${testState.text ?? ""}`;
      } else if (testState.status === "completed") status.textContent = testState.text ?? "事件已注入";
      else if (testState.status === "stopped") status.textContent = "已停止";
      else if (testState.status === "failed" || testState.status === "error") {
        status.textContent = testState.text ?? "运行失败";
      }
      const judge = card.querySelector(".test-judge");
      judge.hidden = testState.status !== "completed";
    }
  };

  const setJudgment = (caseId, value) => {
    testJudgments.set(caseId, value);
    const card = testCases.querySelector(`[data-case-id="${CSS.escape(caseId)}"]`);
    if (!card) return;
    card.dataset.judgment = value;
    const result = card.querySelector(".test-result");
    result.textContent = value === "pass" ? "本轮：通过" : "本轮：失败";
  };

  const renderTestCases = () => {
    if (!testCases) return;
    const set = testSets.find((item) => item.id === activeTestSet);
    testCases.textContent = "";
    if (!set) {
      if (testDescription) testDescription.textContent = "测试集没有载入，请看终端启动错误。";
      if (testCount) testCount.textContent = "0 条";
      testCases.appendChild(el("p", "empty", "没有可运行的测试"));
      return;
    }
    testDescription.textContent = set.description;
    const candidates = [
      ...new Map(
        set.cases
          .filter((item) => item.candidate_id)
          .map((item) => [item.candidate_id, item.candidate_name]),
      ),
    ];
    if (candidates.length && activeCandidate && !candidates.some(([id]) => id === activeCandidate)) {
      activeCandidate = "";
    }
    testCandidate.hidden = candidates.length === 0;
    testCandidate.textContent = "";
    if (candidates.length) {
      const all = el("option", "", "全部主播");
      all.value = "";
      testCandidate.appendChild(all);
      for (const [id, name] of candidates) {
        const option = el("option", "", name);
        option.value = id;
        testCandidate.appendChild(option);
      }
      testCandidate.value = activeCandidate;
    }
    const visible = set.cases.filter(
      (item) => !activeCandidate || item.candidate_id === activeCandidate,
    );
    testCount.textContent = `${visible.length} 条`;
    for (const item of visible) {
      const card = el("article", "test-card");
      card.dataset.caseId = item.id;
      const head = el("div", "test-card-head");
      const titleWrap = el("div");
      titleWrap.appendChild(el("h4", "test-title", item.title));
      const meta = el("div", "test-meta");
      meta.appendChild(el("span", "test-tag", item.group));
      if (item.candidate_name) {
        meta.appendChild(el("span", "test-tag candidate", item.candidate_name));
      }
      meta.appendChild(el("span", "test-duration", `${item.duration_s}s`));
      titleWrap.appendChild(meta);
      head.appendChild(titleWrap);
      const run = el("button", "test-run", "运行");
      run.type = "button";
      run.addEventListener("click", () => {
        if (!send("test.run", { case_id: item.id })) {
          testState = { status: "failed", case_id: item.id, text: "连接断开，测试没有启动" };
          applyTestState();
        }
      });
      head.appendChild(run);
      card.appendChild(head);

      card.appendChild(el("h5", "test-label", "你要做"));
      card.appendChild(el("p", "test-copy", item.operator));
      if (item.focus?.length) {
        card.appendChild(el("h5", "test-label", "重点观察"));
        const focus = el("ul", "test-expected");
        for (const line of item.focus) focus.appendChild(el("li", "", line));
        card.appendChild(focus);
      }
      const eventCount = item.event_count ?? item.events.length;
      card.appendChild(el("h5", "test-label", `会注入（共 ${eventCount} 个事件/动作）`));
      const events = el("div", "test-events");
      if (!item.events.length) {
        events.appendChild(el("span", "test-no-event", "无直播事件，只测麦克风或定时行为"));
      }
      for (const event of item.events) {
        events.appendChild(el("span", "test-event", `${event.at_s}s  ${event.summary}`));
      }
      card.appendChild(events);
      card.appendChild(el("h5", "test-label", "通过标准"));
      const expected = el("ul", "test-expected");
      for (const line of item.expected) expected.appendChild(el("li", "", line));
      card.appendChild(expected);
      if (item.reference) {
        card.appendChild(el("h5", "test-label", "对照说明"));
        card.appendChild(el("p", "test-reference", item.reference));
      }
      card.appendChild(el("p", "test-status"));

      const judge = el("div", "test-judge");
      judge.hidden = true;
      judge.appendChild(el("span", "test-judge-label", "人工判断"));
      const pass = el("button", "test-pass", "通过");
      pass.type = "button";
      pass.addEventListener("click", () => setJudgment(item.id, "pass"));
      const fail = el("button", "test-fail", "失败");
      fail.type = "button";
      fail.addEventListener("click", () => setJudgment(item.id, "fail"));
      judge.appendChild(pass);
      judge.appendChild(fail);
      judge.appendChild(el("span", "test-result"));
      card.appendChild(judge);
      const existing = testJudgments.get(item.id);
      if (existing) {
        card.dataset.judgment = existing;
        card.querySelector(".test-result").textContent =
          existing === "pass" ? "本轮：通过" : "本轮：失败";
      }
      testCases.appendChild(card);
    }
    applyTestState();
  };

  const renderTestSets = () => {
    if (!testSetSwitch) return;
    testSetSwitch.textContent = "";
    for (const set of testSets) {
      const button = el(
        "button",
        "test-set" + (set.id === activeTestSet ? " active" : ""),
        set.title,
      );
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(set.id === activeTestSet));
      button.addEventListener("click", () => {
        activeTestSet = set.id;
        activeCandidate = "";
        renderTestSets();
        renderTestCases();
      });
      testSetSwitch.appendChild(button);
    }
  };

  const loadTestCatalog = (catalog, initialState) => {
    testSets = catalog?.sets ?? [];
    if (!testSets.some((item) => item.id === activeTestSet)) activeTestSet = testSets[0]?.id ?? "";
    testState = initialState ?? { status: "idle", case_id: "" };
    renderTestSets();
    renderTestCases();
  };

  testCandidate?.addEventListener("change", () => {
    activeCandidate = testCandidate.value;
    renderTestCases();
  });
  testStop?.addEventListener("click", () => send("test.stop", {}));
  document.getElementById("live-mock-open")?.addEventListener("click", () => {
    // getDisplayMedia with tab audio needs a real Chrome; the shell opens the
    // page in the external browser, a plain tab just navigates its own way.
    if (window.bilisamaShell?.openLiveMock) window.bilisamaShell.openLiveMock();
    else window.open("live-mock", "_blank", "noopener");
  });

  // A promise-shaped confirm dialog, shared by the assistant page and any
  // future destructive action. Esc is captured before the panel's own
  // close-on-Escape handler.
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmMessage = document.getElementById("confirm-message");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmAccept = document.getElementById("confirm-accept");
  const confirmCancel = document.getElementById("confirm-cancel");
  let settlePendingConfirm = null;
  const confirmAction = ({ title, message, accept = "确认" }) =>
    new Promise((resolve) => {
      if (!confirmDialog) {
        resolve(true);
        return;
      }
      // A second ask while one is open: the first resolves false and its
      // listeners come off — otherwise both promises settle on one click.
      settlePendingConfirm?.(false);
      confirmTitle.textContent = title ?? "确认操作";
      confirmMessage.textContent = message ?? "";
      confirmAccept.textContent = accept;
      confirmDialog.hidden = false;
      const done = (answer) => {
        settlePendingConfirm = null;
        confirmDialog.hidden = true;
        confirmAccept.removeEventListener("click", yes);
        confirmCancel.removeEventListener("click", no);
        confirmDialog.removeEventListener("click", backdrop);
        document.removeEventListener("keydown", esc, true);
        resolve(answer);
      };
      const yes = () => done(true);
      const no = () => done(false);
      const backdrop = (e) => {
        if (e.target === confirmDialog) no();
      };
      const esc = (e) => {
        if (e.key !== "Escape") return;
        e.stopImmediatePropagation();
        no();
      };
      settlePendingConfirm = done;
      confirmAccept.addEventListener("click", yes);
      confirmCancel.addEventListener("click", no);
      confirmDialog.addEventListener("click", backdrop);
      document.addEventListener("keydown", esc, true);
      confirmCancel.focus?.();
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
    /** Browser-tab mode: the corner icon mirrors the in-page sheet. */
    setOnOpenChange(fn) {
      onOpenChange = fn;
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
        if (data.kind === "test") {
          testState = data;
          applyTestState();
          return;
        }
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
        applyAssistants(data.assistants);
        applySkins(data.appearance);
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
      if (data.tests) loadTestCatalog(data.tests, data.test_state);
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
      // The room-event stream and the test card state replay with the rings
      // too; stale rows would double and a finished run would stay painted.
      if (roomEventsEl) {
        roomEventsEl.textContent = "";
        roomEventsEl.appendChild(el("p", "empty", "等待直播间事件…"));
      }
      testState = { status: "idle", case_id: "" };
      applyTestState();
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
