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
});
