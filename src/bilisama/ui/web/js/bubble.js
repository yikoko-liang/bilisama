// The speech bubble above the pet. Three moments in its life:
// appear on the first reply.delta, dismiss 1.5s after the voice state falls
// back to idle (NOT on reply.done — the text stream ends while the speaker
// still holds audio; a bubble that vanishes before the voice stops reads
// broken), and shatter on playback.clear (the line got cut; show it).

const LINGER_MS = 1500;

export function createBubble(el) {
  let open = false;
  let hideTimer = null;

  const hide = () => {
    clearTimeout(hideTimer);
    hideTimer = null;
    open = false;
    el.hidden = true;
    el.classList.remove("show", "shatter");
    el.textContent = "";
  };

  return {
    delta(text) {
      clearTimeout(hideTimer);
      hideTimer = null;
      if (!open) {
        open = true;
        el.textContent = "";
        el.hidden = false;
        el.classList.remove("shatter");
        el.classList.add("show");
      }
      el.textContent += text;
      el.scrollTop = el.scrollHeight;
    },

    onVoiceState(state) {
      if (!open) return;
      if (state === "idle" || state === "offline") {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(hide, LINGER_MS);
      } else {
        // She started listening/thinking again; the bubble stays put until
        // the next reply replaces it or idle finally lands.
        clearTimeout(hideTimer);
        hideTimer = null;
      }
    },

    shatter() {
      if (!open) return;
      clearTimeout(hideTimer);
      el.classList.remove("show");
      el.classList.add("shatter");
      hideTimer = setTimeout(hide, 430);
    },

    hide,
  };
}
