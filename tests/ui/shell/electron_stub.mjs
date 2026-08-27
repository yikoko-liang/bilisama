// A recording stand-in for the `electron` module, so main.mjs can be loaded
// and driven inside a plain node process.
//
// Recorder, not fake, in the same sense as tests/ui/test_audio_page.py's _SPY:
// nothing here re-implements a decision. Every handler main.mjs registers is
// kept exactly as given, and the probe calls the real closures. What the stub
// supplies is only the Electron surface those closures reach for — windows,
// webContents, session — plus a ledger of what was asked of it.
//
// Why a stub at all: `import { app } from "electron"` outside an Electron
// process resolves to a path string, and every call below would be on
// undefined. Spawning real Electron would need a display server and would put
// a live always-on-top window on the machine running the gate.

export const recorded = {
  lockRequested: 0,
  lockGranted: true, // flipped by the probe before importing main.mjs
  quits: 0,
  appEvents: new Map(), // event name -> handler
  windows: [],
  ipc: new Map(), // channel -> handler
  intervals: [],
  // Paths the shell asked the OS to reveal. Recorded rather than ignored so a
  // future test can assert WHAT it revealed, not just that it compiled.
  revealed: [],
};

class FakeWebContents {
  constructor(win) {
    this.win = win;
    this.listeners = new Map();
    this.windowOpenHandler = null;
    this.permissionHandler = null;
    this.session = {
      setPermissionRequestHandler: (handler) => {
        this.permissionHandler = handler;
      },
    };
  }

  on(event, handler) {
    this.listeners.set(event, handler);
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }

  getURL() {
    return this.win.loads.at(-1) ?? "";
  }
}

export class BrowserWindow {
  constructor(options) {
    this.options = options;
    this.loads = [];
    this.destroyed = false;
    this.shown = 0;
    this.focused = 0;
    this.bounds = { x: options.x ?? 0, y: options.y ?? 0 };
    this.size = [options.width, options.height];
    this.listeners = new Map();
    this.webContents = new FakeWebContents(this);
    recorded.windows.push(this);
  }

  loadURL(url) {
    this.loads.push(url);
  }

  isDestroyed() {
    return this.destroyed;
  }

  isMinimized() {
    return false;
  }

  restore() {}

  show() {
    this.shown += 1;
  }

  focus() {
    this.focused += 1;
  }

  setAlwaysOnTop() {}

  setResizable() {}

  setIgnoreMouseEvents() {}

  getPosition() {
    return [this.bounds.x, this.bounds.y];
  }

  getSize() {
    return this.size;
  }

  setBounds(bounds) {
    this.bounds = { x: bounds.x, y: bounds.y };
    this.size = [bounds.width, bounds.height];
  }

  once(event, handler) {
    this.listeners.set(event, handler);
  }

  on(event, handler) {
    this.listeners.set(event, handler);
  }
}

export const app = {
  requestSingleInstanceLock() {
    recorded.lockRequested += 1;
    return recorded.lockGranted;
  },
  quit() {
    recorded.quits += 1;
  },
  whenReady() {
    return Promise.resolve();
  },
  on(event, handler) {
    recorded.appEvents.set(event, handler);
  },
};

export const ipcMain = {
  on(channel, handler) {
    recorded.ipc.set(channel, handler);
  },
};

// Only showItemInFolder is stubbed: it is the one member main.mjs reaches for,
// and a stub that answered more would let the shell grow a dependency nothing
// here is watching.
export const shell = {
  showItemInFolder: (target) => {
    recorded.revealed.push(target);
  },
};

export const screen = {
  getPrimaryDisplay() {
    return { workArea: { x: 0, y: 0, width: 1920, height: 1080 } };
  },
  getDisplayNearestPoint() {
    return { workArea: { x: 0, y: 0, width: 1920, height: 1080 } };
  },
};

export default { app, BrowserWindow, ipcMain, screen, shell };
