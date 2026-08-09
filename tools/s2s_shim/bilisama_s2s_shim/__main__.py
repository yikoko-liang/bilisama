"""Start speech-to-speech with our patches applied.

Patches go on first, then control passes to its own CLI. A failed self-check exits
immediately: upstream drift should surface at startup, not as a silent failure
halfway through a stream.
"""

from __future__ import annotations

import sys

from bilisama_s2s_shim.patches import PatchError, apply_patches


def main() -> int:
    try:
        results = apply_patches()
    except PatchError as exc:
        print(f"[shim] 补丁打不上：{exc}", file=sys.stderr)
        print(
            "[shim] 上游可能改了结构。要么修补丁，要么用零补丁模式：" "BILISAMA_S2S_PATCHES= 空值",
            file=sys.stderr,
        )
        return 3
    except ImportError as exc:
        print(f"[shim] 导入不到 speech-to-speech：{exc}", file=sys.stderr)
        print("[shim] 是不是没在它自己的 venv 里跑？", file=sys.stderr)
        return 4

    for r in results:
        print(f"[shim] {r.name}: {r.detail}", file=sys.stderr)

    from speech_to_speech.cli import main as s2s_main

    s2s_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
