// Sprite skin packs: pet.json + one spritesheet, the qwen-audio-agent /
// Codex desktop-pet format. The animation model below is a faithful port of
// qwen-audio-agent web/src/sprite-orb.js (itself aligned with Codex
// codex-rs/tui/src/pets) — see NOTICE. Track = { frames: [{spriteIndex,
// durationMs}], loopStart, fallback }; loopStart null means one-shot.

import { attachPointer } from "../renderer.js";

const DEFAULT_COLUMNS = 8;
const DEFAULT_FRAME_WIDTH = 192;
const DEFAULT_FRAME_HEIGHT = 208;
const DEFAULT_ROWS = 9;
const V2_ROWS = 11;
const DEFAULT_FPS = 8;
const MAX_FPS = 60;

// Hard caps from plan section 15.12: a hostile or bloated pack degrades to
// the theme skin instead of eating memory.
const MAX_GRID = 32;
const MAX_FRAME_PX = 512;
const MAX_SHEET_PX = 4096;

function idleAnimation() {
  const durations = [1680, 660, 660, 840, 840, 1920];
  return {
    frames: durations.map((durationMs, spriteIndex) => ({ spriteIndex, durationMs })),
    loopStart: 0,
    fallback: "idle",
  };
}

// An action plays three times, then settles into appended idle frames — a
// sustained state never loops the action forever.
function appStateAnimation(rowIndex, frameCount, frameDurationMs, finalFrameDurationMs) {
  const primary = Array.from({ length: frameCount }, (_, column) => ({
    spriteIndex: rowIndex * DEFAULT_COLUMNS + column,
    durationMs: column === frameCount - 1 ? finalFrameDurationMs : frameDurationMs,
  }));
  return {
    frames: [...primary, ...primary, ...primary, ...idleAnimation().frames],
    loopStart: primary.length * 3,
    fallback: "idle",
  };
}

function rowLoopAnimation(rowIndex, frameCount, frameDurationMs, finalFrameDurationMs) {
  const { frames } = appStateAnimation(rowIndex, frameCount, frameDurationMs, finalFrameDurationMs);
  return { frames: frames.slice(0, frameCount), loopStart: 0, fallback: "idle" };
}

export function defaultAnimations() {
  return {
    "idle": idleAnimation(),
    "running-right": appStateAnimation(1, 8, 120, 220),
    "running-left": appStateAnimation(2, 8, 120, 220),
    "waving": appStateAnimation(3, 4, 140, 280),
    "jumping": appStateAnimation(4, 5, 140, 280),
    "failed": appStateAnimation(5, 8, 140, 240),
    "waiting": appStateAnimation(6, 6, 150, 260),
    "running": appStateAnimation(7, 6, 120, 220),
    "review": appStateAnimation(8, 6, 150, 280),
    "working": rowLoopAnimation(2, 8, 120, 220),
    "attention": rowLoopAnimation(4, 5, 140, 280),
  };
}

export function spriteGeometry(manifest = {}) {
  const version = manifest.spriteVersionNumber ?? 1;
  const frame = manifest.frame && typeof manifest.frame === "object"
    ? manifest.frame
    : {
        width: DEFAULT_FRAME_WIDTH,
        height: DEFAULT_FRAME_HEIGHT,
        columns: DEFAULT_COLUMNS,
        rows: version === 2 ? V2_ROWS : DEFAULT_ROWS,
      };
  for (const key of ["width", "height", "columns", "rows"]) {
    if (!Number.isInteger(frame[key]) || frame[key] <= 0) return null;
  }
  if (frame.columns > MAX_GRID || frame.rows > MAX_GRID) return null;
  if (frame.width > MAX_FRAME_PX || frame.height > MAX_FRAME_PX) return null;
  if (frame.width * frame.columns > MAX_SHEET_PX || frame.height * frame.rows > MAX_SHEET_PX) {
    return null;
  }
  return {
    width: frame.width,
    height: frame.height,
    columns: frame.columns,
    rows: frame.rows,
    frameCount: frame.columns * frame.rows,
  };
}

export function frameRect(geometry, spriteIndex) {
  return {
    x: (spriteIndex % geometry.columns) * geometry.width,
    y: Math.floor(spriteIndex / geometry.columns) * geometry.height,
    width: geometry.width,
    height: geometry.height,
  };
}

export function resolveAnimations(manifest = {}, frameCount = 0) {
  const animations = defaultAnimations();
  const specs = manifest.animations;
  if (specs && typeof specs === "object" && !Array.isArray(specs)) {
    for (const [name, spec] of Object.entries(specs)) {
      if (!spec || !Array.isArray(spec.frames) || spec.frames.length === 0) {
        throw new Error(`皮肤动画 ${name} 至少要包含一帧`);
      }
      const fps = spec.fps === undefined ? DEFAULT_FPS : spec.fps;
      if (!Number.isFinite(fps) || fps <= 0 || fps > MAX_FPS) {
        throw new Error(`皮肤动画 ${name} 的 fps 非法`);
      }
      const durationMs = 1000 / fps;
      animations[name] = {
        frames: spec.frames.map((spriteIndex) => ({ spriteIndex, durationMs })),
        loopStart: (spec.loop ?? true) ? 0 : null,
        fallback: spec.fallback || "idle",
      };
    }
  }
  for (const [name, animation] of Object.entries(animations)) {
    for (const frame of animation.frames) {
      if (
        !Number.isInteger(frame.spriteIndex)
        || frame.spriteIndex < 0
        || frame.spriteIndex >= frameCount
      ) {
        throw new Error(`皮肤动画 ${name} 引用了越界的帧索引`);
      }
    }
    if (!animations[animation.fallback]) {
      throw new Error(`皮肤动画 ${name} 的 fallback 不存在`);
    }
  }
  return animations;
}

function totalDuration(animation) {
  return animation.frames.reduce((sum, frame) => sum + frame.durationMs, 0);
}

// Stateless frame resolution: elapsed since track start decides the frame and
// the delay to the next one. One-shots return null past their total; the
// caller switches tracks.
export function frameAtElapsed(animation, elapsedMs) {
  const total = totalDuration(animation);
  if (total <= 0) return null;
  let position = Math.max(0, elapsedMs);
  if (position >= total) {
    if (animation.loopStart === null) return null;
    const introDuration = animation.frames
      .slice(0, animation.loopStart)
      .reduce((sum, frame) => sum + frame.durationMs, 0);
    const loopDuration = total - introDuration;
    if (loopDuration <= 0) return null;
    position = introDuration + ((position - total) % loopDuration);
  }
  let cursor = 0;
  for (const frame of animation.frames) {
    if (position < cursor + frame.durationMs) {
      return {
        spriteIndex: frame.spriteIndex,
        remainingMs: cursor + frame.durationMs - position,
      };
    }
    cursor += frame.durationMs;
  }
  return null;
}

// Our visual states onto the standard tracks. The poke rides the one-shot
// path so it always settles back into whatever the voice state wants.
export function animationForVisual(visual) {
  switch (visual) {
    case "listening":
      return "waiting";
    case "thinking":
      return "review";
    case "speaking":
      return "waving";
    case "offline":
      return "failed";
    default:
      return "idle";
  }
}

// ------------------------------------------------------------ loading

async function fetchManifest(id) {
  // The user's imported packs (skins/ mount, only present when the directory
  // exists) shadow the packaged ones (assets/skins/).
  for (const base of [`skins/${id}/`, `assets/skins/${id}/`]) {
    const response = await fetch(`${base}pet.json`).catch(() => null);
    if (response?.ok) {
      return { base, manifest: await response.json() };
    }
  }
  throw new Error(`找不到皮肤包 ${id}（skins/ 与 assets/skins/ 都没有）`);
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`精灵图加载失败：${url}`));
    image.src = url;
  });
}

export async function mountSprite(mount, id, { onPoke }) {
  const { base, manifest } = await fetchManifest(id);
  const geometry = spriteGeometry(manifest);
  if (!geometry) throw new Error("pet.json 的帧尺寸非法或超出上限");
  const animations = resolveAnimations(manifest, geometry.frameCount);
  const sheet = await loadImage(`${base}${manifest.image || "spritesheet.png"}`);
  if (sheet.naturalWidth !== geometry.width * geometry.columns
      || sheet.naturalHeight !== geometry.height * geometry.rows) {
    throw new Error(
      `精灵图尺寸不符：图是 ${sheet.naturalWidth}x${sheet.naturalHeight}，`
      + `pet.json 说 ${geometry.width * geometry.columns}x${geometry.height * geometry.rows}`,
    );
  }

  const canvas = document.createElement("canvas");
  canvas.className = "sprite-canvas";
  canvas.width = geometry.width;
  canvas.height = geometry.height;
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.objectFit = "contain";
  canvas.style.imageRendering = "pixelated";
  mount.innerHTML = "";
  mount.appendChild(canvas);
  const context = canvas.getContext("2d");
  context.imageSmoothingEnabled = false;

  let visual = "idle";
  let track = { name: "idle", startedAt: performance.now() };
  let timer = null;
  let destroyed = false;

  const setTrack = (name) => {
    if (track.name === name) return;
    track = { name, startedAt: performance.now() };
    draw();
  };

  function draw() {
    if (destroyed) return;
    clearTimeout(timer);
    const animation = animations[track.name] ?? animations.idle;
    let frame = frameAtElapsed(animation, performance.now() - track.startedAt);
    if (frame === null) {
      // One-shot finished: settle into whatever the current visual wants.
      track = { name: animationForVisual(visual), startedAt: performance.now() };
      frame = frameAtElapsed(animations[track.name] ?? animations.idle, 0);
      if (frame === null) return;
    }
    const rect = frameRect(geometry, frame.spriteIndex);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(
      sheet,
      rect.x, rect.y, rect.width, rect.height,
      0, 0, canvas.width, canvas.height,
    );
    timer = setTimeout(draw, Math.max(16, frame.remainingMs));
  }

  const poke = () => {
    // Force-restart even when already jumping: a click deserves a jump.
    track = { name: "jumping", startedAt: performance.now() };
    draw();
  };

  attachPointer(mount, {
    onPoke: () => {
      poke();
      onPoke();
    },
  });

  draw();

  return {
    setState(next) {
      visual = next;
      // Do not cut a running poke short; it settles into the new state.
      if (track.name !== "jumping") setTrack(animationForVisual(next));
    },
    poke,
    destroy() {
      destroyed = true;
      clearTimeout(timer);
      mount.innerHTML = "";
    },
  };
}
