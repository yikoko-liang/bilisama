"""Who gets the microphone, and what happens to the local pair meanwhile.

Two capture streams would fight over the device and two playback queues would
talk over each other, so exactly one client holds them. Nobody holding is the
normal state — `--no-ui`, no shell installed, no page open — and then the
sounddevice pair keeps running exactly as it did before any of this existed.
"""

from __future__ import annotations

from bilisama.ui.audio import AudioBroker


class _Local:
    """Stands in for dev-talk's microphone pump and speaker."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self) -> None:
        self.calls.append("suspend")

    async def resume(self) -> None:
        self.calls.append("resume")


async def test_nobody_holding_leaves_the_local_pair_alone() -> None:
    local = _Local()
    broker = AudioBroker(local=local)
    assert broker.owner is None
    assert broker.local_is_live
    assert local.calls == [], "没人来的时候不该动本地设备"


async def test_a_claim_parks_the_local_pair_and_a_release_brings_it_back() -> None:
    local = _Local()
    broker = AudioBroker(local=local)
    assert await broker.claim("shell", send=lambda _pcm: None)
    assert local.calls == ["suspend"]
    assert not broker.local_is_live

    await broker.release("shell")
    assert local.calls == ["suspend", "resume"]
    assert broker.local_is_live


async def test_the_shell_outranks_a_browser_tab() -> None:
    """Both open is the ordinary developer setup: shell for real, tab to look."""
    local = _Local()
    broker = AudioBroker(local=local)
    assert await broker.claim("browser", send=lambda _pcm: None)
    assert await broker.claim("shell", send=lambda _pcm: None), "壳该顶掉标签页"
    assert broker.owner == "shell"
    # The devices never went back to the local pair in between — restarting
    # PortAudio streams for a handover nobody asked for is pure churn.
    assert local.calls == ["suspend"]


async def test_a_tab_cannot_take_the_devices_from_the_shell() -> None:
    broker = AudioBroker()
    assert await broker.claim("shell", send=lambda _pcm: None)
    assert not await broker.claim("browser", send=lambda _pcm: None)
    assert broker.owner == "shell"


async def test_a_displaced_client_releasing_does_not_park_the_new_one() -> None:
    """The tab hangs up after losing the handover; the shell keeps the devices.

    Without the owner check this is where the sound dies: a stale release from
    the loser would hand everything back to sounddevice while the shell is
    still playing into a socket nobody feeds.
    """
    local = _Local()
    broker = AudioBroker(local=local)
    await broker.claim("browser", send=lambda _pcm: None)
    await broker.claim("shell", send=lambda _pcm: None)

    await broker.release("browser")
    assert broker.owner == "shell"
    assert local.calls == ["suspend"], "本地设备不该在壳还拿着的时候被叫醒"


async def test_downlink_goes_to_the_owner_and_is_dropped_with_nobody_there() -> None:
    heard: list[bytes] = []
    broker = AudioBroker()
    broker.play(b"\x01\x02")  # nobody holds it yet
    assert heard == []

    await broker.claim("shell", send=heard.append)
    broker.play(b"\x03\x04")
    await broker.release("shell")
    broker.play(b"\x05\x06")  # gone again
    assert heard == [b"\x03\x04"], "只有当前持有者该听见，其余丢掉"


async def test_every_change_is_announced() -> None:
    """A tab with no sound has to be able to say why."""
    seen: list[str | None] = []
    broker = AudioBroker(announce=seen.append)
    await broker.claim("browser", send=lambda _pcm: None)
    await broker.claim("shell", send=lambda _pcm: None)
    await broker.release("shell")
    assert seen == ["browser", "shell", None]
