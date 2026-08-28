// The page detects window.bilisamaShell to switch into shell mode: drags move
// the window instead of doing nothing, and the corner icon opens a real panel
// window instead of the in-page sheet. CommonJS because sandboxed preloads
// cannot be ES modules.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bilisamaShell", {
  dragStart: (x, y) => ipcRenderer.send("pet:drag-start", x, y),
  dragMove: (x, y) => ipcRenderer.send("pet:drag-move", x, y),
  dragEnd: () => ipcRenderer.send("pet:drag-end"),
  openPanel: () => ipcRenderer.send("pet:open-panel"),
  // The page asks the window to hug the mounted skin (bottom-anchored).
  fit: (w, h) => ipcRenderer.send("pet:fit", w, h),
  // ...and to stop swallowing clicks everywhere the pet is not.
  setInteractive: (on) => ipcRenderer.send("pet:interactive", Boolean(on)),
  // No argument on purpose: the main process computes the log path itself.
  revealLog: () => ipcRenderer.send("shell:reveal-log"),
  // The exit handshake: the page confirmed with the streamer already.
  close: () => ipcRenderer.send("pet:close-shell"),
  // The pet's settings button glows while the panel window is open. Returns
  // an unsubscribe, though the pet page never bothers — it lives as long as
  // the window does.
  onPanelState: (callback) => {
    const listener = (_event, open) => callback(open);
    ipcRenderer.on("panel:state", listener);
    return () => ipcRenderer.removeListener("panel:state", listener);
  },
});
