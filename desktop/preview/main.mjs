// The desktop-pet shell: a transparent always-on-top window around the page
// dev-talk already serves. It attaches, it never launches — P2 stays the
// authority on its own lifecycle. This directory is deliberately disposable:
// stage 7's real desktop/ tree (supervisor, packaging, signing) replaces it,
// while the page and the protocol it displays carry forward unchanged.
//
// Window recipe follows qwen-audio-agent's proven desktop orb (main.mjs:507)
// and airi's stage-tamagotchi: frame:false + transparent + alwaysOnTop
// 'floating' + skipTaskbar + backgroundThrottling:false.

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { app, BrowserWindow, ipcMain, screen } from "electron";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PET_WIDTH = 240;
const PET_HEIGHT = 340;
const PANEL_WIDTH = 420;
const PANEL_HEIGHT = 680;
const POLL_MS = 2000;

let petWindow = null;
let panelWindow = null;
let currentUrl = null;
let dragOrigin = null;

// ------------------------------------------------------------ endpoint

function endpointPath() {
  const base = process.env.XDG_DATA_HOME || path.join(homedir(), ".local", "share");
  return path.join(base, "bilisama", "ui", "endpoint.json");
}

function discoverUrl() {
  if (process.env.BILISAMA_UI_URL) return process.env.BILISAMA_UI_URL;
  try {
    const { url, pid } = JSON.parse(readFileSync(endpointPath(), "utf-8"));
    if (typeof url !== "string" || !url.startsWith("http://127.0.0.1:")) return null;
    if (Number.isInteger(pid)) {
      process.kill(pid, 0); // throws when that dev-talk is gone
    }
    return url;
  } catch {
    return null;
  }
}

// The waiting page is inline data:, so the shell needs no served assets of
// its own before dev-talk exists.
const WAITING_PAGE = `data:text/html;charset=utf-8,${encodeURIComponent(`
  <body style="margin:0;height:100vh;display:grid;place-items:center;
               background:rgba(24,25,28,.86);border-radius:18px;
               font-family:system-ui,'PingFang SC',sans-serif;color:#98958b">
    <div style="text-align:center;font-size:13px;line-height:1.9">
      <div style="font-size:30px;opacity:.6">◌</div>
      等待 dev-talk 启动…<br>
      <span style="font-size:11px;opacity:.7">bilisama dev-talk --director</span>
    </div>
  </body>`)}`;

// ------------------------------------------------------------ windows

function createPetWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  petWindow = new BrowserWindow({
    width: PET_WIDTH,
    height: PET_HEIGHT,
    x: workArea.x + workArea.width - PET_WIDTH - 24,
    y: workArea.y + workArea.height - PET_HEIGHT - 24,
    frame: false,
    transparent: true,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: true,
    hasShadow: false,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // The window is almost always unfocused; throttling would starve the
      // WebSocket handlers and freeze the animations.
      backgroundThrottling: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });
  petWindow.setAlwaysOnTop(true, "floating");
  petWindow.once("ready-to-show", () => petWindow.show());
  petWindow.on("closed", () => {
    petWindow = null;
  });
  harden(petWindow);
}

function openPanelWindow() {
  if (!currentUrl) return;
  if (panelWindow && !panelWindow.isDestroyed()) {
    panelWindow.focus();
    return;
  }
  panelWindow = new BrowserWindow({
    width: PANEL_WIDTH,
    height: PANEL_HEIGHT,
    title: "BiliSama 面板",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  panelWindow.loadURL(`${currentUrl}#panel`);
  panelWindow.on("closed", () => {
    panelWindow = null;
  });
  harden(panelWindow);
}

function harden(win) {
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, target) => {
    // Only our own loopback pages; anything else stays where it is.
    if (!currentUrl || !target.startsWith(originOf(currentUrl))) event.preventDefault();
  });
  win.webContents.session.setPermissionRequestHandler((_wc, _permission, callback) => {
    callback(false); // the shell needs no mic, camera or anything else
  });
}

function originOf(url) {
  return new URL(url).origin;
}

// ------------------------------------------------------------ attach loop

function watchEndpoint() {
  const attach = () => {
    const url = discoverUrl();
    if (url === currentUrl) return;
    currentUrl = url;
    if (!petWindow || petWindow.isDestroyed()) return;
    if (url) {
      petWindow.loadURL(url);
      if (panelWindow && !panelWindow.isDestroyed()) panelWindow.loadURL(`${url}#panel`);
    } else {
      petWindow.loadURL(WAITING_PAGE);
      panelWindow?.close();
    }
  };
  attach();
  if (!currentUrl && petWindow) petWindow.loadURL(WAITING_PAGE);
  // Token and port rotate with every dev-talk run; polling the endpoint file
  // makes restarts seamless instead of leaving a dead page.
  setInterval(attach, POLL_MS);
}

// ------------------------------------------------------------ drag IPC

function fromPet(event) {
  return petWindow && !petWindow.isDestroyed() && event.sender === petWindow.webContents;
}

ipcMain.on("pet:drag-start", (event, x, y) => {
  if (!fromPet(event) || typeof x !== "number" || typeof y !== "number") return;
  const [wx, wy] = petWindow.getPosition();
  dragOrigin = { mx: x, my: y, wx, wy };
});

ipcMain.on("pet:drag-move", (event, x, y) => {
  if (!fromPet(event) || !dragOrigin || typeof x !== "number" || typeof y !== "number") return;
  petWindow.setPosition(
    Math.round(dragOrigin.wx + x - dragOrigin.mx),
    Math.round(dragOrigin.wy + y - dragOrigin.my),
  );
});

ipcMain.on("pet:drag-end", (event) => {
  if (fromPet(event)) dragOrigin = null;
});

ipcMain.on("pet:open-panel", (event) => {
  if (fromPet(event)) openPanelWindow();
});

// ------------------------------------------------------------ lifecycle

app.whenReady().then(() => {
  createPetWindow();
  watchEndpoint();
});

app.on("window-all-closed", () => {
  app.quit();
});
