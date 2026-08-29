// The speech bubble above the pet. Three moments in its life:
// appear on the first reply.delta, dismiss 1.5s after the voice state falls
// back to idle (NOT on reply.done — the text stream ends while the speaker
// still holds audio; a bubble that vanishes before the voice stops reads
// broken), and shatter on playback.clear (the line got cut; show it).
//
// The shatter is its own little state: while it plays, voice-state noise
// (the barge-in that caused it arrives as listening within ~100ms) must not
// cancel the cleanup — the .shatter animation ends at opacity 0 with
// `forwards`, so a bubble left in that class is invisible forever. A fresh
// delta during the shatter starts a NEW bubble from scratch instead.

const LINGER_MS = 1500;
const SHATTER_MS = 430;

export function createBubble(el) {
  let open = false;
  let hideTimer = null;
  let shatterTimer = null;
  let replyEnded = false;
  let transient = false; // a one-shot line on its own timer; voice state is noise to it

  const hide = () => {
    clearTimeout(hideTimer);
    clearTimeout(shatterTimer);
    hideTimer = null;
    shatterTimer = null;
    open = false;
    replyEnded = false;
    transient = false;
    el.hidden = true;
    el.classList.remove("show", "shatter");
    el.textContent = "";
  };

  return {
    showTransient(text, lingerMs = 2600) {
      // One whole line at once — control feedback and full-reply fallbacks,
      // not a stream. Self-dismisses on its own timer; the transient flag is
      // what keeps onVoiceState's hands off it — without it, the first
      // listening/speaking frame cleared hideTimer and the line stuck
      // on screen indefinitely.
      hide();
      open = true;
      replyEnded = true;
      transient = true;
      el.textContent = text;
      el.hidden = false;
      el.classList.add("show");
      hideTimer = setTimeout(hide, lingerMs);
    },

    delta(text) {
      if (shatterTimer !== null) {
        // Mid-shatter: the old line is dead, this is a new one.
        hide();
      }
      clearTimeout(hideTimer);
      hideTimer = null;
      transient = false;
      if (!open || replyEnded) {
        // A finished reply is still on screen (the bubble outlives the text
        // stream by design) — replace it rather than appending, or replies
        // arriving inside the linger window concatenate without end.
        open = true;
        replyEnded = false;
        el.textContent = "";
        el.hidden = false;
        el.classList.remove("shatter");
        el.classList.add("show");
      }
      el.textContent += text;
      el.scrollTop = el.scrollHeight;
    },

    endReply() {
      // The text stream is done; the audio may still be playing, so the bubble
      // stays. It is the NEXT delta that clears it (see above).
      if (open) replyEnded = true;
    },

    onVoiceState(state) {
      if (!open || shatterTimer !== null || transient) return;
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
      if (!open || shatterTimer !== null) return;
      clearTimeout(hideTimer);
      hideTimer = null;
      el.classList.remove("show");
      el.classList.add("shatter");
      shatterTimer = setTimeout(hide, SHATTER_MS);
    },

    hide,
  };
}
