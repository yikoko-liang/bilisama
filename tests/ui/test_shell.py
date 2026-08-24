"""Structural tests for the Electron shell (desktop/preview/main.mjs).

Why this tier exists: the shell was 279 lines with nothing automated behind
them, and one of those lines is the only gate standing between a page read off
disk and the microphone (main.mjs's setPermissionRequestHandler). Plan §15.13
asked for exactly two of the tests below — 「本 origin 放行、别的 origin 仍被
拒，各一条测试」 — and the delivery never wrote them.

How, without a display server: tests/ui/shell/probe.mjs loads main.mjs in a
plain node process with `electron` resolved to a recording stub, then calls the
handlers main.mjs registered and prints what they answered. Every verdict below
is a REAL closure from main.mjs deciding about a real input; the stub supplies
only the Electron surface those closures reach for. Same idea as
tests/unit/test_dependency_direction.py reading the tree instead of running it,
one level up: this reads the decisions rather than the source text.

What stays manual (CONTRIBUTING「界面改动的人工验收」): window physics —
transparency, click-through, drag, the panel window actually appearing.

Unmarked on purpose, so the gate's unit step picks it up: no browser, no
devices, no display. It skips out loud when node is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
_PROBE_DIR = _HERE / "shell"
_SHELL = _HERE.parents[1] / "desktop" / "preview" / "main.mjs"

_NODE = shutil.which("node")
if _NODE is None:  # pragma: no cover - reported out loud, like the s2s tier
    pytest.skip(
        "node 没装，壳的结构测试跑不了：装 node 20+ 或 `brew install node`",
        allow_module_level=True,
    )


def _probe(*, url: str | None = "http://127.0.0.1:7777", lock: bool = True) -> dict[str, Any]:
    """Run main.mjs under the stub and return its report.

    Args:
        url: What BILISAMA_UI_URL pins the shell to; None leaves it unset and
            points the endpoint lookup at an empty directory, which is the
            shell's waiting state (no dev-talk yet).
        lock: Whether the single-instance lock is granted — False is the second
            `npm start`.
    """
    assert _NODE is not None
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PROBE_SINGLE_LOCK": "1" if lock else "0",
        # No endpoint.json under here, so discoverUrl() finds nothing. A real
        # $HOME would let a dev-talk running on this machine leak into the run.
        "XDG_DATA_HOME": str(_PROBE_DIR / "no-such-data-home"),
    }
    if url is not None:
        env["BILISAMA_UI_URL"] = url
    done = subprocess.run(
        [_NODE, "--import", "./register.mjs", "./probe.mjs"],
        cwd=_PROBE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise AssertionError(f"探针跑不起来（{done.returncode}）：\n{done.stderr}")
    report: dict[str, Any] = json.loads(done.stdout)
    return report


# ------------------------------------------------------------ the microphone


def test_the_shell_hands_the_microphone_to_its_own_page() -> None:
    """§15.13's first required test: our own origin is let through.

    The shell is where audio lives now (commit a99cd2c). If this gate ever
    refuses, the streamer is back to headphones and the page can only say
    「拿不到麦克风」 without saying why.
    """
    report = _probe()
    for name in ("pet", "panel"):
        window = report[name]
        assert window is not None, f"{name} 窗口没开出来"
        assert window["permission"]["ownOriginMedia"] is True, f"{name} 窗口拿不到麦克风"


def test_the_shell_refuses_the_microphone_to_every_other_origin() -> None:
    """§15.13's second required test, and the reason the gate is not just
    `callback(permission === "media")`.

    The URL this window follows is read off disk. A squatter on the recorded
    port, or a page that navigated away, must not inherit the grant — and the
    comparison is on parsed origins because 127.0.0.1:7777 is a prefix of
    127.0.0.1:77771.
    """
    asked = _probe()["pet"]["permission"]
    assert asked["otherPortMedia"] is False, "另一个端口的页面也拿到了麦克风"
    assert asked["offHostMedia"] is False, "站外页面也拿到了麦克风"
    assert asked["unparseableMedia"] is False, "地址解析不出来时放行了麦克风"


def test_the_shell_refuses_everything_that_is_not_the_microphone() -> None:
    """The grant is one permission wide. An always-on-top window holding the
    preload's IPC bridge should not be able to collect geolocation or push
    notifications on the way past."""
    asked = _probe()["pet"]["permission"]
    assert asked["ownOriginGeolocation"] is False, "自家页面要定位也给了"
    assert asked["ownOriginNotifications"] is False, "自家页面要通知也给了"


def test_the_waiting_shell_grants_nothing_at_all() -> None:
    """Before dev-talk exists there is no origin to be, and the window is
    showing an inline data: card. `wanted === null` has to mean refuse — a
    null-compares-equal slip here would grant the microphone to whatever the
    window happens to be displaying."""
    report = _probe(url=None)
    pet = report["pet"]
    assert pet["loads"], "等待中的窗口什么都没加载"
    assert pet["loads"][-1].startswith("data:"), f"等待中的窗口不在等待卡上：{pet['loads']}"
    for name, answer in pet["permission"].items():
        assert answer is False, f"还没连上 dev-talk 就放行了 {name}"


# ------------------------------------------------------------ navigation


def test_navigation_and_redirect_both_stay_on_our_own_origin() -> None:
    """A 30x never fires will-navigate, which is why both are wired to the same
    comparison: without will-redirect, a squatter on the recorded port could
    send this frameless always-on-top window anywhere."""
    pet = _probe()["pet"]
    for event in ("navigate", "redirect"):
        moves = pet[event]
        assert moves["ownOrigin"] is True, f"{event}：自家页面之间的跳转被拦了"
        assert moves["otherPort"] is False, f"{event}：跳到别的端口没有被拦"
        assert moves["offHost"] is False, f"{event}：跳到站外没有被拦"
    assert pet["navigate"]["unparseable"] is False, "跳到解析不出来的地址没有被拦"
    assert pet["windowOpen"] == "deny", "window.open 没有被拒"


# ------------------------------------------------------------ single instance


def test_a_second_shell_quits_instead_of_opening_a_deaf_window() -> None:
    """Two shells, one microphone.

    AudioBroker turns the second one away at the door — 「同级不顶同级」
    (ui/audio.py:161) — but the control socket's sticky audio.owner still says
    owner=shell, so the second window's panel reads 「麦克风和扬声器已接管」
    over a window that captures nothing. The streamer cannot tell which of two
    identical pets is the live one. So the second process must not get that
    far.
    """
    report = _probe(lock=False)
    assert report["lockRequested"] >= 1, "壳压根没申请单实例锁"
    assert report["quits"] >= 1, "第二个壳没有退出"
    assert report["windowCount"] == 0, f"第二个壳还是开了 {report['windowCount']} 扇窗"


def test_the_running_shell_comes_forward_when_a_second_start_is_tried() -> None:
    """Quitting silently would look like `npm start` doing nothing at all. The
    instance that holds the lock answers by showing itself."""
    report = _probe()
    assert report["hasSecondInstanceHandler"], "没有 second-instance 处理，第二次启动毫无反应"
    assert report["petShown"] >= 1, "第二次启动时，在跑的那扇窗没有显示出来"
    assert report["petFocused"] >= 1, "第二次启动时，在跑的那扇窗没有被聚焦"


# ------------------------------------------------------------ the file itself


def test_the_probe_reads_the_shipped_shell() -> None:
    """A guard for the harness: if desktop/preview/ moves, every test above
    would pass on nothing at all."""
    assert _SHELL.exists(), f"壳的入口不在这儿了：{_SHELL}"
