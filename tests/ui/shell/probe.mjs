// Loads desktop/preview/main.mjs against the recording stub, drives the
// handlers it registered, and prints one JSON report on stdout.
//
// The assertions live in tests/ui/test_shell.py; this file only produces
// evidence. Everything it reports is the answer of a REAL closure from
// main.mjs — the permission gate, the navigation gate, the single-instance
// branch — called with an input the shell can genuinely see.
//
// Run as: node --import ./register.mjs probe.mjs

import { recorded } from "./electron_stub.mjs";

const OTHER = "http://127.0.0.1:9999"; // a squatter on another loopback port
const OFF_HOST = "https://example.invalid";

recorded.lockGranted = process.env.PROBE_SINGLE_LOCK !== "0";

// The import has side effects: main.mjs asks for the lock, and (when it gets
// it) createPetWindow + watchEndpoint run off app.whenReady().
await import(new URL("../../../desktop/preview/main.mjs", import.meta.url).href);
// One macrotask is enough for whenReady's .then chain; setTimeout(0) lands
// after every queued microtask.
await new Promise((done) => setTimeout(done, 0));

/** Ask one window's permission gate about one origin. */
function permission(win, url, name) {
  const handler = win.webContents.permissionHandler;
  if (!handler) return null;
  let answer = null;
  handler({ getURL: () => url }, name, (allowed) => {
    answer = allowed;
  });
  return answer;
}

/** Ask one window's navigation gate; true means the move was allowed. */
function navigation(win, event, target) {
  const handler = win.webContents.listeners.get(event);
  if (!handler) return null;
  let prevented = false;
  handler({ preventDefault: () => (prevented = true) }, target);
  return !prevented;
}

function battery(win) {
  if (!win) return null;
  const mine = win.loads.at(-1) ?? "";
  return {
    loads: win.loads,
    permission: {
      // The one thing the shell is allowed to hand out, and only to itself.
      ownOriginMedia: permission(win, mine, "media"),
      // Same host, different port: a startsWith check would have said yes.
      otherPortMedia: permission(win, OTHER, "media"),
      offHostMedia: permission(win, OFF_HOST, "media"),
      unparseableMedia: permission(win, "not a url", "media"),
      // Everything else stays refused even from our own page.
      ownOriginGeolocation: permission(win, mine, "geolocation"),
      ownOriginNotifications: permission(win, mine, "notifications"),
    },
    navigate: {
      ownOrigin: navigation(win, "will-navigate", `${mine}#panel`),
      otherPort: navigation(win, "will-navigate", OTHER),
      offHost: navigation(win, "will-navigate", OFF_HOST),
      unparseable: navigation(win, "will-navigate", "not a url"),
    },
    redirect: {
      ownOrigin: navigation(win, "will-redirect", mine),
      otherPort: navigation(win, "will-redirect", OTHER),
      offHost: navigation(win, "will-redirect", OFF_HOST),
    },
    windowOpen: win.webContents.windowOpenHandler
      ? win.webContents.windowOpenHandler({ url: OFF_HOST }).action
      : null,
  };
}

const pet = recorded.windows[0] ?? null;

// The panel is a second hardened window, opened over IPC by the pet page.
const openPanel = recorded.ipc.get("pet:open-panel");
if (pet && openPanel) openPanel({ sender: pet.webContents });
const panel = recorded.windows[1] ?? null;

// A second `npm start`: the running instance is told, and has to make itself
// findable instead of leaving the newcomer's dead window on screen.
const secondInstance = recorded.appEvents.get("second-instance");
if (secondInstance) secondInstance({}, ["electron", "."], process.cwd());

console.log(
  JSON.stringify({
    lockRequested: recorded.lockRequested,
    quits: recorded.quits,
    windowCount: recorded.windows.length,
    hasSecondInstanceHandler: Boolean(secondInstance),
    petShown: pet ? pet.shown : 0,
    petFocused: pet ? pet.focused : 0,
    pet: battery(pet),
    panel: battery(panel),
  }),
);

// watchEndpoint left a poll interval running; nothing else keeps this process
// from being the shell that never exits.
process.exit(0);
