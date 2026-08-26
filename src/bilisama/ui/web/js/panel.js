// The five-page control centre. Every visible edit is sent to the running
// backend and comes back through panel.state; no control asks for a restart.

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
  entry: "进房",
  follow: "关注",
  like: "点赞",
  share: "分享",
  proactive: "主动话题",
  background_result: "后台结果",
};

const FEED_WHO = {
  super_chat: "SC", sc: "SC", gift: "礼物", danmaku: "弹幕",
  guard_buy: "上舰", vip_enter: "VIP 进房", entry: "进房",
  follow: "关注", like: "点赞", share: "分享", room_state: "房间状态",
  transcript: "主播语音", reply: "助手回复", proactive: "主动话题",
  background_result: "后台结果", voice: "主播语音",
};

const GUARD_LABEL = {
  governor: "总督",
  admiral: "提督",
  captain: "舰长",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clock(ts) {
  return typeof ts === "string" && ts.length >= 19 ? ts.slice(11, 19) : "";
}

export function createPanel({ send, onOpenChange = () => {} }) {
  const panel = document.getElementById("panel");
  const scrim = document.getElementById("scrim");
  const corner = document.getElementById("corner");
  const nameEl = document.getElementById("p-name");
  const stateEl = document.getElementById("p-state");
  const healthEl = document.getElementById("health");
  const matrixEl = document.getElementById("speak-matrix");
  const timelineEl = document.getElementById("timeline");
  const loglinesEl = document.getElementById("loglines");
  const levelSel = document.getElementById("log-level");
  const pauseBtn = document.getElementById("log-pause");
  const injectForm = document.getElementById("inject");
  const injectInput = document.getElementById("inject-input");
  const liveMockOpen = document.getElementById("live-mock-open");
  const testSetSwitch = document.getElementById("test-set-switch");
  const testDescription = document.getElementById("test-description");
  const testCandidate = document.getElementById("test-candidate");
  const testCount = document.getElementById("test-count");
  const testStop = document.getElementById("test-stop");
  const testCases = document.getElementById("test-cases");
  const roomEventsEl = document.getElementById("room-events");
  const audioEnabled = document.getElementById("audio-input-enabled");
  const audioMeter = document.getElementById("audio-signal-meter");
  const audioMeterValue = document.getElementById("audio-signal-value");
  const noiseInput = document.getElementById("noise-sensitivity");
  const noiseValue = document.getElementById("noise-value");
  const roomIdInput = document.getElementById("room-id");
  const roomConnect = document.getElementById("room-connect");
  const roomDisconnect = document.getElementById("room-disconnect");
  const roomStatus = document.getElementById("room-status");
  const streamerName = document.getElementById("streamer-name");
  const streamerNameSave = document.getElementById("streamer-name-save");
  const streamIntro = document.getElementById("stream-intro");
  const roomInfoSave = document.getElementById("room-info-save");
  const roomInfoHint = document.getElementById("room-info-hint");
  const chattiness = document.getElementById("chattiness");
  const replyLength = document.getElementById("reply-length");
  const danmakuWindow = document.getElementById("danmaku-window");
  const danmakuWindowValue = document.getElementById("danmaku-window-value");
  const giftMedium = document.getElementById("gift-medium");
  const giftHigh = document.getElementById("gift-high");
  const assistantCards = document.getElementById("assistant-cards");
  const assistantEditor = document.getElementById("assistant-editor");
  const assistantTitle = document.getElementById("assistant-editor-title");
  const assistantProfileSwitch = document.getElementById("assistant-profile-switch");
  const assistantIdentity = document.getElementById("assistant-identity");
  const assistantPersonality = document.getElementById("assistant-personality");
  const assistantSave = document.getElementById("assistant-save");
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmMessage = document.getElementById("confirm-message");
  const confirmCancel = document.getElementById("confirm-cancel");
  const confirmAccept = document.getElementById("confirm-accept");

  const panelOnly = document.body.classList.contains("panel-only");
  let isOpen = panelOnly;
  let healthTimer = null;
  let configLoaded = false; // retained by the legacy /config renderer, no longer opened
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
    onOpenChange(true);
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
    onOpenChange(false);
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

  liveMockOpen.addEventListener("click", () => {
    if (window.bilisamaShell?.openLiveMock) {
      window.bilisamaShell.openLiveMock();
      return;
    }
    location.href = new URL("live-mock", location.href).href;
  });

  const speakBoxes = new Map();
  const unsupportedSpeak = new Set(["follow", "like", "share"]);

  const renderSpeak = (speak) => {
    // Update in place once built: a rebuild on every panel.state echo would
    // drop keyboard focus mid-click and flicker the matrix.
    for (const [key, value] of Object.entries(speak ?? {})) {
      if (key === "vip_enter" || key === "background_result") continue;
      const existing = speakBoxes.get(key);
      if (existing) {
        existing.disabled = unsupportedSpeak.has(key);
        existing.checked = existing.disabled
          ? false
          : key === "entry" ? Boolean(value && speak.vip_enter) : Boolean(value);
        continue;
      }
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.checked = key === "entry" ? Boolean(value && speak.vip_enter) : Boolean(value);
      box.disabled = unsupportedSpeak.has(key);
      if (box.disabled) box.checked = false;
      box.addEventListener("change", () => {
        const patch = key === "entry"
          ? { entry: box.checked, vip_enter: box.checked }
          : { [key]: box.checked };
        send("panel.set", { speak: patch });
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
    if (send("console.line", { text, as_live: true })) {
      injectInput.value = "";
    } else {
      // Disconnected: keep the text instead of silently eating it.
      feedEntry({ kind: "system", text: "连接断开，这条没发出去" });
    }
  });

  // ------------------------------------------------------------ system page

  const sendConfig = (path, value) => send("panel.set", { config: { path, value } });

  audioEnabled.addEventListener("change", () => {
    send("panel.set", { audio: { input_enabled: audioEnabled.checked } });
  });
  noiseInput.addEventListener("input", () => {
    noiseValue.textContent = noiseInput.value;
  });
  noiseInput.addEventListener("change", () => {
    send("panel.set", { audio: { noise_sensitivity: Number(noiseInput.value) } });
  });
  roomConnect.addEventListener("click", () => {
    const roomId = Number(roomIdInput.value.trim());
    if (!Number.isInteger(roomId) || roomId <= 0) {
      roomStatus.textContent = "请输入有效的直播间 ID";
      roomStatus.className = "connection-status error";
      return;
    }
    roomConnect.disabled = true;
    roomStatus.textContent = `正在检测房间 ${roomId}…`;
    roomStatus.className = "connection-status";
    send("panel.set", { room: { action: "connect", room_id: roomId } });
  });
  roomDisconnect.addEventListener("click", () => {
    roomDisconnect.disabled = true;
    roomStatus.textContent = "正在断开直播间事件流…";
    roomStatus.className = "connection-status";
    send("panel.set", { room: { action: "disconnect" } });
  });
  let roomInfoOriginal = { streamerName: "", streamIntro: "" };
  let roomEventsRoomId = null;
  let streamerNamePending = false;
  let streamIntroPending = false;
  const refreshRoomInfoDirty = () => {
    const streamerDirty = streamerName.value.trim() !== roomInfoOriginal.streamerName;
    const introDirty = streamIntro.value.trim() !== roomInfoOriginal.streamIntro;
    streamerNameSave.disabled = !streamerDirty || streamerNamePending;
    roomInfoSave.disabled = !introDirty || streamIntroPending;
    if (streamerNamePending || streamIntroPending) roomInfoHint.textContent = "正在保存…";
    else if (streamerDirty || introDirty) roomInfoHint.textContent = "有未保存修改";
    else roomInfoHint.textContent = "修改后保存，下一次回复起生效";
  };
  streamerName.addEventListener("input", refreshRoomInfoDirty);
  streamIntro.addEventListener("input", refreshRoomInfoDirty);
  const saveRoomInfo = ({ saveStreamer = false, saveIntro = false } = {}) => {
    const streamer = streamerName.value.trim();
    if (saveStreamer) streamerNamePending = true;
    if (saveIntro) streamIntroPending = true;
    refreshRoomInfoDirty();
    send("panel.set", {
      room: {
        action: "save_info",
        streamer_name: streamer,
        stream_intro: saveIntro ? streamIntro.value.trim() : roomInfoOriginal.streamIntro,
      },
    });
  };
  streamerNameSave.addEventListener("click", () => saveRoomInfo({ saveStreamer: true }));
  roomInfoSave.addEventListener("click", () => saveRoomInfo({ saveIntro: true }));
  chattiness.addEventListener("change", () => {
    sendConfig("interaction.chattiness", chattiness.value);
  });
  replyLength.addEventListener("change", () => {
    sendConfig("interaction.reply_length", replyLength.value);
  });
  danmakuWindow.addEventListener("input", () => {
    danmakuWindowValue.textContent = `${danmakuWindow.value}s`;
  });
  danmakuWindow.addEventListener("change", () => {
    sendConfig("interaction.danmaku.window_s", Number(danmakuWindow.value));
  });
  giftMedium.addEventListener("change", () => {
    if (giftMedium.value.trim()) {
      sendConfig("interaction.gift_battery_medium", Number(giftMedium.value));
    }
  });
  giftHigh.addEventListener("change", () => {
    if (giftHigh.value.trim()) {
      sendConfig("interaction.gift_battery_high", Number(giftHigh.value));
    }
  });
  for (const box of document.querySelectorAll("[data-entry-group]")) {
    box.addEventListener("change", () => {
      sendConfig(`interaction.entry_welcome.${box.dataset.entryGroup}`, box.checked);
    });
  }

  const applySystemState = (data) => {
    if (data.audio) {
      const audio = data.audio;
      if (typeof audio.input_enabled === "boolean") audioEnabled.checked = audio.input_enabled;
      if (Number.isFinite(audio.noise_sensitivity)) {
        noiseInput.value = String(audio.noise_sensitivity);
        noiseValue.textContent = String(audio.noise_sensitivity);
      }
    }
    if (data.room) {
      const room = data.room;
      const configuredRoomId = Number(room.room_id) || 0;
      if (roomEventsRoomId !== null && configuredRoomId !== roomEventsRoomId) {
        roomEventsEl.replaceChildren(el("p", "empty", "等待新直播间事件…"));
      }
      roomEventsRoomId = configuredRoomId;
      if (document.activeElement !== roomIdInput) roomIdInput.value = room.room_id || "";
      roomConnect.disabled = false;
      roomDisconnect.disabled = !room.connected;
      roomStatus.textContent = room.connected
        ? `已连接直播间 ${room.active_room_id || room.room_id}`
        : room.error || "尚未连接直播间";
      roomStatus.className = `connection-status${room.connected ? " connected" : room.error ? " error" : ""}`;
    }
    if (data.persona && data.room) {
      const incoming = {
        streamerName: data.persona.streamer_name ?? "",
        streamIntro: data.room.stream_intro ?? "",
      };
      const streamerDirty = streamerName.value.trim() !== roomInfoOriginal.streamerName;
      if (document.activeElement !== streamerName && (!streamerDirty || streamerNamePending)) {
        streamerName.value = incoming.streamerName;
        roomInfoOriginal.streamerName = incoming.streamerName;
        streamerNamePending = false;
      }
      const introDirty = streamIntro.value.trim() !== roomInfoOriginal.streamIntro;
      if (document.activeElement !== streamIntro && (!introDirty || streamIntroPending)) {
        streamIntro.value = incoming.streamIntro;
        roomInfoOriginal.streamIntro = incoming.streamIntro;
        streamIntroPending = false;
      }
      refreshRoomInfoDirty();
    }
    if (data.interaction) {
      const interaction = data.interaction;
      chattiness.value = interaction.chattiness ?? chattiness.value;
      replyLength.value = interaction.reply_length ?? replyLength.value;
      if (Number.isFinite(interaction.danmaku_window_s)) {
        danmakuWindow.value = String(interaction.danmaku_window_s);
        danmakuWindowValue.textContent = `${danmakuWindow.value}s`;
      }
      if (Number.isFinite(interaction.gift_battery_medium)) {
        giftMedium.value = String(interaction.gift_battery_medium);
      }
      if (Number.isFinite(interaction.gift_battery_high)) {
        giftHigh.value = String(interaction.gift_battery_high);
      }
      if (interaction.entry_welcome) {
        for (const box of document.querySelectorAll("[data-entry-group]")) {
          if (typeof interaction.entry_welcome[box.dataset.entryGroup] === "boolean") {
            box.checked = interaction.entry_welcome[box.dataset.entryGroup];
          }
        }
      }
    }
  };

  const liveKinds = new Set([
    "danmaku", "gift", "super_chat", "guard_buy", "vip_enter", "entry",
    "follow", "like", "share", "room_state",
  ]);

  const roomEvent = (data) => {
    if (!liveKinds.has(data.kind)) return;
    roomEventsEl.querySelector(".empty")?.remove();
    const row = el("div", "room-event");
    row.appendChild(el("time", "", clock(data.ts)));
    row.appendChild(el("span", "event-tag", FEED_WHO[data.kind] ?? data.kind));
    const gift = data.gift?.name
      ? `${data.gift.name} ×${data.gift.num ?? 1}`
      : "";
    const identity = [
      data.identity && data.identity !== "anon" ? data.identity : "",
      Number(data.user_level) > 0 ? `用户 Lv.${data.user_level}` : "",
      Number(data.wealth_level) > 0 ? `财富 Lv.${data.wealth_level}` : "",
      data.is_admin ? "房管" : "",
      GUARD_LABEL[data.guard_level] ?? "",
      data.medal?.name
        ? `${data.medal.this_room ? "本房" : "外房"}粉丝牌 ${data.medal.name} Lv.${data.medal.level}`
        : "",
    ].filter(Boolean).join(" · ");
    const body = [data.name ?? "一位观众", identity, data.text || gift].filter(Boolean).join(" ｜ ");
    row.appendChild(el("span", "event-body", body));
    roomEventsEl.appendChild(row);
    while (roomEventsEl.children.length > 100) roomEventsEl.firstChild.remove();
    roomEventsEl.scrollTop = roomEventsEl.scrollHeight;
  };

  let confirmResolve = null;

  const closeConfirmation = (accepted) => {
    if (confirmDialog.hidden) return;
    confirmDialog.hidden = true;
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve?.(accepted);
  };

  const confirmAction = ({ title, message, accept = "确认" }) => {
    if (confirmResolve) closeConfirmation(false);
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmAccept.textContent = accept;
    confirmDialog.hidden = false;
    confirmCancel.focus();
    return new Promise((resolve) => {
      confirmResolve = resolve;
    });
  };

  confirmCancel.addEventListener("click", () => closeConfirmation(false));
  confirmAccept.addEventListener("click", () => closeConfirmation(true));
  confirmDialog.addEventListener("click", (event) => {
    if (event.target === confirmDialog) closeConfirmation(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || confirmDialog.hidden) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    closeConfirmation(false);
  }, true);

  // ------------------------------------------------------------ assistant market

  let assistants = [];
  let selectedAssistant = "";
  let selectedProfile = "";
  let editorOriginal = { identity: "", personality: "" };

  const editorDirty = () => assistantIdentity.value !== editorOriginal.identity
    || assistantPersonality.value !== editorOriginal.personality;

  const updateSaveState = () => {
    assistantSave.disabled = !selectedAssistant || !selectedProfile || !editorDirty();
  };

  const showProfile = (item, profile, { activate = false } = {}) => {
    selectedProfile = profile.id;
    editorOriginal = {
      identity: profile.identity ?? "",
      personality: profile.personality ?? "",
    };
    assistantIdentity.value = editorOriginal.identity;
    assistantPersonality.value = editorOriginal.personality;
    if (activate && !profile.current) {
      send("panel.set", {
        assistant: {
          action: "select_profile",
          id: item.id,
          profile: profile.id,
        },
      });
    }
    updateSaveState();
    renderAssistants();
    renderProfiles(item);
  };

  const showAssistant = (item) => {
    selectedAssistant = item.id;
    assistantEditor.hidden = false;
    assistantTitle.textContent = `${item.name} · 人设配置`;
    const profiles = Array.isArray(item.profiles) ? item.profiles : [];
    const profile = profiles.find((candidate) => candidate.id === selectedProfile)
      ?? profiles.find((candidate) => candidate.current)
      ?? profiles[0];
    if (profile) showProfile(item, profile);
    else renderProfiles(item);
  };

  function renderAssistants() {
    assistantCards.textContent = "";
    for (const item of assistants) {
      const card = el("article", `assistant-card${selectedAssistant === item.id ? " selected" : ""}`);
      card.dataset.assistantId = item.id;
      card.tabIndex = 0;
      const preview = el("div", `assistant-preview ${item.id}`);
      card.appendChild(preview);
      if (item.current) card.appendChild(el("span", "current-tag", "当前"));
      card.appendChild(el("h2", "", item.name));
      card.appendChild(el("p", "", item.description));
      card.addEventListener("click", () => showAssistant(item));
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") showAssistant(item);
      });
      assistantCards.appendChild(card);
    }
  }

  function renderProfiles(item) {
    assistantProfileSwitch.textContent = "";
    for (const profile of item.profiles ?? []) {
      const button = el(
        "button",
        `assistant-profile${selectedProfile === profile.id ? " selected" : ""}`,
      );
      button.type = "button";
      button.dataset.profileId = profile.id;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(selectedProfile === profile.id));
      const copy = el("span", "assistant-profile__copy");
      copy.appendChild(el("strong", "", profile.name));
      copy.appendChild(el("small", "", profile.description));
      button.appendChild(copy);
      if (profile.current) button.appendChild(el("span", "current-tag", "当前"));
      button.addEventListener("click", async () => {
        if (profile.current) {
          showProfile(item, profile);
          return;
        }
        const accepted = await confirmAction({
          title: "切换 Mia 人设？",
          message: `确认切换到「${profile.name}」？下一条回复起实时生效，不会重连语音服务。`,
          accept: "确认切换",
        });
        if (accepted) showProfile(item, profile, { activate: true });
      });
      assistantProfileSwitch.appendChild(button);
    }
  }

  const applyAssistants = (items) => {
    assistants = Array.isArray(items) ? items : [];
    renderAssistants();
    if (!selectedAssistant) return;
    const selected = assistants.find((item) => item.id === selectedAssistant);
    if (!selected) return;
    if (editorDirty()) {
      renderProfiles(selected);
      return;
    }
    showAssistant(selected);
  };

  assistantIdentity.addEventListener("input", updateSaveState);
  assistantPersonality.addEventListener("input", updateSaveState);
  assistantSave.addEventListener("click", async () => {
    if (!selectedAssistant || !selectedProfile || !editorDirty()) return;
    const profile = assistants
      .find((item) => item.id === selectedAssistant)?.profiles
      ?.find((item) => item.id === selectedProfile);
    const accepted = await confirmAction({
      title: "保存人设修改？",
      message: `确认覆盖「${profile?.name ?? selectedProfile}」的人设文档？保存后下一条回复起实时生效。`,
      accept: "确认保存",
    });
    if (!accepted) return;
    if (assistantIdentity.value !== editorOriginal.identity) {
      send("panel.set", { assistant: { action: "save", id: selectedAssistant, profile: selectedProfile, anchor: "identity", text: assistantIdentity.value } });
    }
    if (assistantPersonality.value !== editorOriginal.personality) {
      send("panel.set", { assistant: { action: "save", id: selectedAssistant, profile: selectedProfile, anchor: "personality", text: assistantPersonality.value } });
    }
    editorOriginal = {
      identity: assistantIdentity.value,
      personality: assistantPersonality.value,
    };
    assistantSave.disabled = true;
  });

  // ------------------------------------------------------------ chat tab

  const pushEntry = (node) => {
    timelineEl.querySelector(".empty")?.remove();
    timelineEl.appendChild(node);
    while (timelineEl.children.length > FEED_CAP) timelineEl.firstChild.remove();
    timelineEl.parentElement.scrollTop = timelineEl.parentElement.scrollHeight;
  };

  const referenceText = (reference) => {
    if (!reference?.kind) return "";
    const label = FEED_WHO[reference.kind] ?? reference.kind;
    const money = reference.value_cny ? ` ¥${Math.round(reference.value_cny)}` : "";
    const battery = reference.gift?.total_battery
      ? ` ${reference.gift.total_battery} 电池`
      : "";
    const gift = reference.gift?.name
      ? `${reference.gift.name} ×${reference.gift.num ?? 1}`
      : "";
    const actor = reference.name ? ` · ${reference.name}${reference.kind === "gift" ? battery : money}` : "";
    const body = reference.text || gift;
    return `引用 ${label}${actor}${body ? `：${body}` : ""}`;
  };

  const feedEntry = (data) => {
    const kind = data.kind ?? "system";
    const visualKind = kind === "super_chat" ? "sc" : kind;
    const entry = el("div", `entry ${visualKind}`);
    entry.appendChild(el("span", "when", clock(data.ts)));
    if (kind === "verdict") {
      const reason = data.reason ? `(${data.reason})` : "";
      entry.appendChild(
        el("span", "", `${data.source} → ${data.outcome}@${data.phase}${reason}`),
      );
    } else if (kind === "reply") {
      entry.appendChild(el("span", "event-pill", "助手回复"));
      entry.appendChild(el("span", "who", "她"));
      const cited = referenceText(data.reference);
      if (cited) entry.appendChild(el("div", "reply-reference", cited));
      const status = data.status === "completed" ? "" : `〔${data.status}〕`;
      entry.appendChild(el("span", "reply-copy", `${data.text || "（无文本）"}${status}`));
    } else if (kind === "transcript") {
      entry.appendChild(el("span", "event-pill", "主播语音"));
      entry.appendChild(el("span", "who", "你"));
      entry.appendChild(el("span", "", data.text ?? ""));
    } else if (FEED_WHO[kind]) {
      const money = data.value_cny ? ` ¥${Math.round(data.value_cny)}` : "";
      const battery = data.gift?.total_battery ? ` ${data.gift.total_battery} 电池` : "";
      const gift = data.gift?.name ? `${data.gift.name} ×${data.gift.num ?? 1}` : "";
      entry.appendChild(el("span", "event-pill", FEED_WHO[kind]));
      entry.appendChild(el("span", "who", `${data.name ?? "?"}${kind === "gift" ? battery : money}`));
      entry.appendChild(el("span", "", data.text || gift));
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

  // ------------------------------------------------------------ test console

  let testSets = [];
  let activeTestSet = "functional";
  let activeCandidate = "";
  let testState = { status: "idle", case_id: "" };
  const testJudgments = new Map();

  const isTestRunning = () => ["running", "event"].includes(testState.status);

  const applyTestState = () => {
    const running = isTestRunning();
    testStop.disabled = !running;
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
      else if (testState.status === "failed") status.textContent = testState.text ?? "运行失败";
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
    const set = testSets.find((item) => item.id === activeTestSet);
    testCases.textContent = "";
    if (!set) {
      testDescription.textContent = "测试集没有载入，请看终端启动错误。";
      testCount.textContent = "0 条";
      testCases.appendChild(el("p", "empty", "没有可运行的测试"));
      return;
    }
    testDescription.textContent = set.description;
    const candidates = [...new Map(
      set.cases.filter((item) => item.candidate_id).map((item) => [item.candidate_id, item.candidate_name]),
    )];
    if (candidates.length && !candidates.some(([id]) => id === activeCandidate)) {
      activeCandidate = candidates[0][0];
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
      if (item.candidate_name) meta.appendChild(el("span", "test-tag candidate", item.candidate_name));
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
      if (!item.events.length) events.appendChild(el("span", "test-no-event", "无直播事件，只测麦克风或定时行为"));
      for (const event of item.events) {
        events.appendChild(el("span", "test-event", `${event.at_s}s  ${event.summary}`));
      }
      card.appendChild(events);
      card.appendChild(el("h5", "test-label", "通过标准"));
      const expected = el("ul", "test-expected");
      for (const line of item.expected) expected.appendChild(el("li", "", line));
      card.appendChild(expected);
      if (item.reference) {
        card.appendChild(el("h5", "test-label", "N.E.K.O 对照"));
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
        card.querySelector(".test-result").textContent = existing === "pass" ? "本轮：通过" : "本轮：失败";
      }
      testCases.appendChild(card);
    }
    applyTestState();
  };

  const renderTestSets = () => {
    testSetSwitch.textContent = "";
    for (const set of testSets) {
      const button = el("button", "test-set" + (set.id === activeTestSet ? " active" : ""), set.title);
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

  testCandidate.addEventListener("change", () => {
    activeCandidate = testCandidate.value;
    renderTestCases();
  });
  testStop.addEventListener("click", () => send("test.stop", {}));

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
    startHealth();
  }

  return {
    handleFrame(event, data) {
      if (event === "event.feed") {
        if (data.kind === "test") {
          testState = data;
          applyTestState();
        } else {
          feedEntry(data);
          roomEvent(data);
        }
      }
      else if (event === "log.line") logEntry(data.line ?? "");
      else if (event === "audio.level") {
        const level = Math.max(0, Math.min(100, Number(data.level) || 0));
        audioMeter.style.width = `${level}%`;
        audioMeterValue.textContent = String(Math.round(level));
      }
      else if (event === "panel.state") {
        renderSpeak(data.speak);
        applySpeakToConfig(data.speak);
        applySystemState(data);
        if (Array.isArray(data.assistants)) applyAssistants(data.assistants);
      }
    },
    setHello(data) {
      nameEl.textContent = data.persona?.name ?? "BiliSama";
      if (data.panel) this.handleFrame("panel.state", data.panel);
      loadTestCatalog(data.tests, data.test_state);
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
      roomEventsEl.textContent = "";
      roomEventsEl.appendChild(el("p", "empty", "等待直播间事件…"));
      loglinesEl.textContent = "";
      testState = { status: "idle", case_id: "" };
      applyTestState();
    },
  };
}
