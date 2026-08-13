// The CSS robot: the nothing-can-404 last resort when even the packaged tofu
// skin fails to load. State looks are pure CSS keyed off #stage[data-visual];
// this file only owns what CSS cannot: the random blink, and spawning spark
// particles at the click point.

import { attachPointer } from "../renderer.js";

const ROBOT_HTML = `
  <div class="robot">
    <div class="antenna"><i class="stem"></i><i class="tip"></i></div>
    <div class="head">
      <div class="screen">
        <i class="eye eye-l"></i><i class="eye eye-r"></i>
        <span class="think"><i></i><i></i><i></i></span>
        <span class="eq"><i></i><i></i><i></i></span>
        <i class="scan"></i>
      </div>
    </div>
    <div class="torso"></div>
    <span class="zzz">z</span>
  </div>
  <div class="ring"></div>
`;

export function mountFallback(mount, { onPoke }) {
  mount.innerHTML = ROBOT_HTML;
  const robot = mount.querySelector(".robot");

  // Blink on a 2-6s random timer: irregularity is what reads as alive.
  let blinkTimer = null;
  const scheduleBlink = () => {
    blinkTimer = setTimeout(() => {
      robot.classList.add("blink");
      setTimeout(() => robot.classList.remove("blink"), 130);
      scheduleBlink();
    }, 2000 + Math.random() * 4000);
  };
  scheduleBlink();

  const spawnSparks = (e) => {
    const rect = mount.getBoundingClientRect();
    const count = 1 + Math.floor(Math.random() * 3);
    for (let i = 0; i < count; i += 1) {
      const spark = document.createElement("span");
      spark.className = "spark";
      spark.textContent = "✦";
      spark.style.left = `${e.clientX - rect.left + (Math.random() * 14 - 7)}px`;
      spark.style.top = `${e.clientY - rect.top - 6}px`;
      spark.style.setProperty("--sx", `${Math.random() * 24 - 12}px`);
      mount.appendChild(spark);
      spark.addEventListener("animationend", () => spark.remove());
    }
  };

  const poke = (e) => {
    robot.classList.remove("poked");
    // Restart the animation even mid-play: force a reflow between classes.
    void robot.offsetWidth;
    robot.classList.add("poked");
    if (e) spawnSparks(e);
  };

  attachPointer(mount, {
    onPoke: (e) => {
      poke(e);
      onPoke();
    },
  });

  return {
    setState() {}, // CSS reads data-visual off the stage; nothing to do here
    poke,
    destroy() {
      clearTimeout(blinkTimer);
      mount.innerHTML = "";
    },
  };
}
