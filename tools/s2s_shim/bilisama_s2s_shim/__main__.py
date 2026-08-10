"""Start speech-to-speech with our patches applied.

Patches go on first, then control passes to its own CLI. A failed self-check exits
immediately: upstream drift should surface at startup, not as a silent failure
halfway through a stream.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any

from bilisama_s2s_shim.patches import PatchError, apply_patches


def _pin_hosts(spec: str) -> list[str]:
    """Pin hostnames to fixed IPs for this process only.

    BILISAMA_RESOLVE="host=ip,host2=ip2" is for split-tunnel setups: a proxy
    that hijacks system DNS answers every name with its own fake IP, so an
    intranet LLM endpoint resolves into the wrong tunnel. Pinning here keeps
    the fix inside the serve process — the system proxy stays untouched, and
    the OS routing table sends the real intranet IP through the VPN interface.
    The caller still has to exempt the host from proxy env (NO_PROXY).
    """
    pins = {}
    for pair in spec.split(","):
        host, _, ip = pair.strip().partition("=")
        if host and ip:
            pins[host.lower()] = ip
    original = socket.getaddrinfo

    def pinned(host: Any, *args: Any, **kwargs: Any) -> Any:
        target = pins.get(str(host).lower(), host)
        return original(target, *args, **kwargs)

    socket.getaddrinfo = pinned
    return [f"{h} -> {ip}" for h, ip in pins.items()]


def main() -> int:
    resolve_spec = os.environ.get("BILISAMA_RESOLVE", "")
    if resolve_spec:
        for line in _pin_hosts(resolve_spec):
            print(f"[shim] 域名钉死：{line}", file=sys.stderr)
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
