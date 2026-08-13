// One WebSocket to the dev-talk process, with exponential backoff reconnect.
// The server replays sticky state and recent history on every connect; the
// page clears its panel on reconnect so the replay lands on a clean slate.

export function connect({ onFrame, onStatus }) {
  const url = `ws://${location.host}${location.pathname.replace(/\/$/, "")}/ws`;
  let sock = null;
  let closed = false;
  let attempt = 0;
  let retryTimer = null;

  const open = () => {
    if (closed) return;
    sock = new WebSocket(url);
    sock.onopen = () => {
      attempt = 0;
      onStatus(true);
    };
    sock.onmessage = (message) => {
      let frame;
      try {
        frame = JSON.parse(message.data);
      } catch {
        return; // not ours; drop
      }
      if (frame && typeof frame.event === "string") {
        onFrame(frame.event, frame.data ?? {});
      }
    };
    sock.onclose = () => {
      onStatus(false);
      if (closed) return;
      // 0.5s -> 8s cap, with jitter so several tabs do not stampede.
      const delay = Math.min(8000, 500 * 2 ** attempt) * (0.7 + Math.random() * 0.6);
      attempt += 1;
      retryTimer = setTimeout(open, delay);
    };
    sock.onerror = () => sock.close();
  };

  open();

  return {
    /** True when the frame went onto the wire; false while disconnected. */
    send(event, data = {}) {
      if (sock && sock.readyState === WebSocket.OPEN) {
        sock.send(JSON.stringify({ event, data }));
        return true;
      }
      return false;
    },
    close() {
      closed = true;
      clearTimeout(retryTimer);
      sock?.close();
    },
  };
}
