// The renderer seam. Two implementations today (theme = built-in robot,
// sprite = pet.json skin pack); Live2D becomes the third in stage 5. The
// factory is the single switch point — everything else talks to the returned
// object: { setState(visual), poke(), destroy() }.

import { mountTheme } from "./skins/theme.js";
import { mountSprite } from "./skins/sprite.js";

export async function createRenderer(mount, avatar, hooks) {
  if (avatar?.renderer === "sprite" && avatar.model_id) {
    try {
      return await mountSprite(mount, avatar.model_id, hooks);
    } catch (err) {
      // Missing pack, malformed manifest, oversized sheet: degrade, and say
      // so where the panel's log tab can see it.
      console.warn(`皮肤包 ${avatar.model_id} 加载失败，退回内置形象：`, err);
    }
  }
  return mountTheme(mount, hooks);
}

// Pointer handling shared by every skin: <4px of movement is a poke, more is
// a drag (forwarded to the desktop shell when one is hosting us). Not
// -webkit-app-region: drag — that swallows clicks and the poke dies.
export function attachPointer(el, { onPoke }) {
  const shell = window.bilisamaShell;
  let start = null;
  let moved = false;

  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    el.setPointerCapture(e.pointerId);
    start = { x: e.screenX, y: e.screenY };
    moved = false;
    shell?.dragStart?.(e.screenX, e.screenY);
  });

  el.addEventListener("pointermove", (e) => {
    if (!start) return;
    if (!moved && Math.hypot(e.screenX - start.x, e.screenY - start.y) >= 4) {
      moved = true;
      el.classList.add("dragging");
    }
    if (moved) shell?.dragMove?.(e.screenX, e.screenY);
  });

  const end = (e) => {
    if (!start) return;
    const wasDrag = moved;
    start = null;
    moved = false;
    el.classList.remove("dragging");
    shell?.dragEnd?.();
    if (!wasDrag && e.type === "pointerup") onPoke(e);
  };

  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}
