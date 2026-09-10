"""Stage 1's acceptance criteria, run against the fake server.

Plan section 9, stage 1: the same client must work across all three
Capabilities profiles, text deltas must arrive through it on the s2s shape,
and the section 3.3 failure modes must be survivable — including the wedge,
with recovery.

Everything here drives the real RealtimeClient / adapters against
MockRealtimeServer. No test reaches into either side's private state: what the
adapter sends is asserted through server.recorded, what the client understood
through its LinkEvents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.providers.hosted import HostedLink
from bilisama.realtime.providers.s2s import S2SLink
from tests.fakes.mock_realtime import Fault, MockRealtimeServer, Script

AnyLink = S2SLink | HostedLink


async def _next_event(
    events: AsyncIterator[link.LinkEvent],
    wanted: type | tuple[type, ...],
    *,
    timeout: float = 5.0,
) -> link.LinkEvent:
    """Pull events until one of the wanted types arrives; fail loudly on time."""
    seen: list[str] = []

    async def pull() -> link.LinkEvent:
        async for event in events:
            if isinstance(event, wanted):
                return event
            seen.append(type(event).__name__)
        raise AssertionError("event stream ended")

    try:
        return await asyncio.wait_for(pull(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(f"没等到 {wanted}，只看到 {seen}") from None


@contextmanager
def _capturing(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Collect one logger's records at DEBUG, fields and all.

    A handler rather than caplog, for two reasons: the assertions are about the
    structured fields (`first_frame_ms`, `status`), which live on
    `record.fields` and never reach the formatted message; and caplog's own
    handler is installed on the root, which `obs.logging.setup()` clears.
    """
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    sink = _Sink()
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.setLevel(previous)
        logger.removeHandler(sink)


def _named(records: list[logging.LogRecord], event: str) -> list[dict[str, Any]]:
    """Every logged `event`'s fields, in order."""
    out: list[dict[str, Any]] = []
    for record in records:
        if record.getMessage() != event:
            continue
        fields = getattr(record, "fields", {})
        out.append(fields if isinstance(fields, dict) else {})
    return out


def _profiles() -> list[tuple[str, caps_mod.Capabilities, dia.Codec, Callable[[str], AnyLink]]]:
    return [
        ("s2s", caps_mod.S2S, dia.GA, S2SLink),
        (
            "dashscope",
            caps_mod.DASHSCOPE,
            dia.BETA,
            lambda url: HostedLink(url, ProviderName.DASHSCOPE),
        ),
        (
            "openai_ga",
            caps_mod.OPENAI_GA,
            dia.GA,
            lambda url: HostedLink(url, ProviderName.OPENAI_GA),
        ),
    ]


# ------------------------------------------------------------ the three shapes


@pytest.mark.parametrize(
    ("name", "caps", "codec", "make"),
    _profiles(),
    ids=[p[0] for p in _profiles()],
)
async def test_one_client_speaks_all_three_shapes(
    name: str,
    caps: caps_mod.Capabilities,
    codec: dia.Codec,
    make: Callable[[str], AnyLink],
) -> None:
    """Stage 1's headline criterion: connect, push context, request a reply and
    watch it complete — same code, three provider shapes."""
    async with MockRealtimeServer(caps=caps, codec=codec, script=Script(delta_chunks=2)) as server:
        linkobj = make(server.url)
        await linkobj.connect()
        try:
            await linkobj.set_context("你是一个直播间的 AI 伴播。")
            await linkobj.add_context_item("[弹幕] 观众A: 主播好")
            handle = await linkobj.request_reply(link.ReplySpec(instructions="打个招呼"))
            events = linkobj.events()
            started = await _next_event(events, link.ReplyStarted)
            assert isinstance(started, link.ReplyStarted)
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
            assert done.handle is handle
        finally:
            await linkobj.aclose()


async def test_text_deltas_arrive_through_the_s2s_link() -> None:
    """The acceptance wording verbatim: 能拿到 response.output_text.delta —
    which only happens when the create names the text modality (§15.8)."""
    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=3)) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await linkobj.request_reply(link.ReplySpec())
            events = linkobj.events()
            delta = await _next_event(events, link.ReplyTextDelta)
            assert isinstance(delta, link.ReplyTextDelta)
            assert delta.text
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.text == server.script.reply_text
        finally:
            await linkobj.aclose()


async def test_hosted_replies_carry_audio() -> None:
    """The other half of the hybrid: owns_tts providers answer in PCM."""
    async with MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=Script(delta_chunks=2)) as server:
        linkobj = HostedLink(server.url, ProviderName.OPENAI_GA)
        await linkobj.connect()
        try:
            await linkobj.request_reply(link.ReplySpec())
            events = linkobj.events()
            audio = await _next_event(events, link.ReplyAudioDelta)
            assert isinstance(audio, link.ReplyAudioDelta)
            assert audio.pcm
        finally:
            await linkobj.aclose()


# ------------------------------------------------------------ the rules


async def test_injections_survive_an_open_speculative_window() -> None:
    """The architecture decision as a client test: with the wedge armed and the
    window open, the adapter's reply still completes, because every injection
    goes out-of-band by construction."""
    script = Script(faults={Fault.WEDGE_ON_INJECTION})
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await server.speech_started()  # window open — the trap is armed
            await linkobj.request_reply(link.ReplySpec(instructions="谢谢 SC"))
            done = await _next_event(linkobj.events(), link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
        finally:
            await linkobj.aclose()


async def test_the_adapter_never_sends_the_forbidden_frames() -> None:
    """Rules 1 and 8 as recorded fact: after a full conversation, the server saw
    no in-band injection and no buffer commit/clear — the static gate plan
    section 10.1 asks for, run against real traffic."""
    async with MockRealtimeServer(caps=caps_mod.S2S) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await linkobj.set_context("人设")
            await linkobj.push_audio(b"\x00\x00" * 320)
            await linkobj.add_context_item("[弹幕] 你好")
            await linkobj.request_reply(link.ReplySpec(instructions="回一句"))
            done = await _next_event(linkobj.events(), link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
        finally:
            await linkobj.aclose()

        for forbidden in (
            "input_audio_buffer.commit",
            "input_audio_buffer.clear",
            "output_audio_buffer.clear",
        ):
            assert server.recorded.count(forbidden) == 0
        creates = [e for e in server.recorded.events if e.get("type") == "response.create"]
        assert creates, "the reply request never reached the wire"
        for frame in creates:
            assert (frame.get("response") or {}).get("conversation") == "none"
            assert (frame.get("response") or {}).get("output_modalities") == ["text"]
            assert "input" not in (frame.get("response") or {})


async def test_two_requests_serialise_on_the_single_slot() -> None:
    """Rule 5: the second create waits for the first reply's done. The server's
    own guard would answer with an error; the client must never trigger it."""
    async with MockRealtimeServer(
        caps=caps_mod.S2S, script=Script(delta_chunks=2, delta_interval_s=0.02)
    ) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            first = asyncio.create_task(linkobj.request_reply(link.ReplySpec(instructions="一")))
            second = asyncio.create_task(linkobj.request_reply(link.ReplySpec(instructions="二")))
            events = linkobj.events()
            done_one = await _next_event(events, link.ReplyDone)
            done_two = await _next_event(events, link.ReplyDone, timeout=10.0)
            assert isinstance(done_one, link.ReplyDone)
            assert isinstance(done_two, link.ReplyDone)
            assert done_one.status is link.ReplyStatus.COMPLETED
            assert done_two.status is link.ReplyStatus.COMPLETED
            await asyncio.gather(first, second)
        finally:
            await linkobj.aclose()
        assert server.recorded.count("error") == 0, "the slot guard fired — serialisation failed"


async def test_watchdog_frees_a_wedged_slot() -> None:
    """Rule 2: a create that never answers gets cancelled by the client, the
    handle comes back TIMED_OUT, and the link accepts new work afterwards."""
    clock = FakeClock()
    script = Script(faults={Fault.STALL_RESPONSE})
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url, clock=clock, watchdog_s=25.0)
        await linkobj.connect()
        try:
            handle = await linkobj.request_reply(link.ReplySpec(instructions="有人吗"))
            await asyncio.sleep(0.05)  # let the watchdog task register its sleeper
            await clock.advance(26.0)
            done = await _next_event(linkobj.events(), link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.TIMED_OUT
            assert done.handle is handle
            assert handle.stale
            # The done surfaces locally while the cancel still rides the socket;
            # wait for the server to actually receive it before asserting.
            for _ in range(200):
                if server.recorded.count("response.cancel"):
                    break
                await asyncio.sleep(0.01)
            assert server.recorded.count("response.cancel") == 1
        finally:
            await linkobj.aclose()


async def test_barge_in_cancels_and_late_frames_stay_dead() -> None:
    """Interruption end to end: done(cancelled) arrives before speech_started
    (the backwards order upstream really uses), the handle goes stale, and the
    partial text survives on the done event."""
    async with MockRealtimeServer(
        caps=caps_mod.S2S, script=Script(delta_chunks=5, delta_interval_s=0.05)
    ) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            handle = await linkobj.request_reply(link.ReplySpec(instructions="讲个长故事"))
            events = linkobj.events()
            await _next_event(events, link.ReplyTextDelta)
            await server.barge_in()
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.CANCELLED
            assert done.handle is handle
            assert handle.stale
            assert done.text, "the partial text should survive on the done event"
            assert len(done.text) < len(server.script.reply_text)
            started = await _next_event(events, link.SpeechStarted)
            assert isinstance(started, link.SpeechStarted)
        finally:
            await linkobj.aclose()


async def test_implicit_reply_is_booked_from_its_first_frame() -> None:
    """Rule 4: the server's own VAD turn never announces itself; the client
    books it on the first delta and frees the slot on its done, so the next
    request does not fight ghosts."""
    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=2)) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            events = linkobj.events()
            await server.emit_implicit_reply()
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
            # Minted from a frame nobody asked for: the handle says so, which
            # is what lets the voice gate tell her own turn from ours.
            assert done.handle.implicit
            # The slot must be free again: a fresh request completes too.
            ours = await linkobj.request_reply(link.ReplySpec(instructions="接一句"))
            assert not ours.implicit
            done2 = await _next_event(events, link.ReplyDone)
            assert isinstance(done2, link.ReplyDone)
            assert done2.status is link.ReplyStatus.COMPLETED
            assert done2.handle is ours
        finally:
            await linkobj.aclose()


async def test_hosted_implicit_created_books_the_slot() -> None:
    """Hosted endpoints ANNOUNCE their VAD replies with response.created —
    the client must book the slot on that frame, so an intent arriving during
    the implicit turn queues instead of drawing the server's slot error (C8).
    Modelling every provider as silent hid this for a whole stage.

    DashScope only: on the OpenAI GA shape out-of-band replies are exempt from
    the slot, so there is nothing to queue behind — the exemption is its own
    test elsewhere."""
    script = Script(delta_chunks=2, delta_interval_s=0.05)
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA, script=script) as server:
        linkobj: AnyLink = HostedLink(server.url, ProviderName.DASHSCOPE)
        await linkobj.connect()
        try:
            events = linkobj.events()
            # Held implicit: created goes out, no token yet — the exact window
            # where an unbooked slot would let a request through to its death.
            await server.emit_implicit_reply(hold=True)
            # Let the created frame land before requesting; a request racing
            # the frame itself is the documented FIFO-pairing residual (C1),
            # not what this test pins.
            await asyncio.sleep(0.05)
            request = asyncio.create_task(
                linkobj.request_reply(link.ReplySpec(instructions="接一句"))
            )
            await asyncio.sleep(0.1)
            assert not request.done(), "the request must queue behind the announced implicit turn"
            await server.release_pending_reply()
            first = await _next_event(events, link.ReplyDone)
            assert isinstance(first, link.ReplyDone)
            # Announced by created rather than by a first frame, still hers.
            assert first.handle.implicit
            ours = await request
            assert not ours.implicit
            second = await _next_event(events, link.ReplyDone)
            assert isinstance(second, link.ReplyDone)
            assert second.status is link.ReplyStatus.COMPLETED
            assert server.recorded.count("error") == 0, "the slot guard fired — client sent early"
        finally:
            await linkobj.aclose()


async def test_late_frames_for_a_settled_reply_stay_buried() -> None:
    """The tombstone: a delta straggling in AFTER its reply settled must not
    re-book the slot as a phantom implicit turn (C6). Without the graveyard,
    rule 4's first-frame booking resurrects every settled rid."""
    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            events = linkobj.events()
            await linkobj.request_reply(link.ReplySpec(instructions="说一句"))
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            # The same rid, one frame too late. The fake clears its books on
            # done, so replay the first minted id — deterministic "resp_1"
            # (_mint_response_id counts from 1).
            await server.send(dia.ServerEvent.TEXT_DELTA, response_id="resp_1", delta="迟到的字")
            # A fresh request must find the slot free and complete; along the
            # way, no event from the ghost may surface.
            await linkobj.request_reply(link.ReplySpec(instructions="再说一句"))
            while True:
                event = await _next_event(events, (link.ReplyTextDelta, link.ReplyDone))
                if isinstance(event, link.ReplyTextDelta):
                    assert "迟到的字" not in event.text, "a buried reply's frame surfaced"
                    continue
                assert isinstance(event, link.ReplyDone)
                assert event.status is link.ReplyStatus.COMPLETED
                break
        finally:
            await linkobj.aclose()


async def test_connection_loss_settles_records_and_says_so() -> None:
    """A dead transport mid-reply: every open record fails, the slot frees,
    and the consumer hears connection_lost — not a silent hang."""
    script = Script(delta_chunks=8, delta_interval_s=0.1)
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            handle = await linkobj.request_reply(link.ReplySpec(instructions="讲个长故事"))
            events = linkobj.events()
            await _next_event(events, link.ReplyTextDelta)
            await server.drop_connection()
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.FAILED
            assert done.handle is handle
            assert handle.stale
            down = await _next_event(events, link.LinkDown)
            assert isinstance(down, link.LinkDown)
            assert down.reason.startswith("connection_closed")
        finally:
            await linkobj.aclose()


async def test_deferred_item_ack_is_not_retried() -> None:
    """During a reply the server defers item acks and flushes them later; the
    adapter sends each item exactly once — retrying is how duplicates happen."""
    # Deferral is unconditional on the fake, as upstream
    # (handlers/conversation.py:48-52 has no flag behind it).
    script = Script(delta_chunks=3, delta_interval_s=0.05)
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await linkobj.request_reply(link.ReplySpec(instructions="说话"))
            events = linkobj.events()
            await _next_event(events, link.ReplyTextDelta)
            await linkobj.add_context_item("[SC ¥30] 阿强: 主播好")
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
        finally:
            await linkobj.aclose()
        assert server.recorded.count("conversation.item.create") == 1


async def test_user_transcript_reaches_l3_as_the_streamer() -> None:
    """The streamer's words surface as UserTranscript*, never as a reply."""
    async with MockRealtimeServer(caps=caps_mod.S2S) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await server.user_transcript("主播说了这句话")
            got = await _next_event(linkobj.events(), link.UserTranscriptDone)
            assert isinstance(got, link.UserTranscriptDone)
            assert got.text == "主播说了这句话"
        finally:
            await linkobj.aclose()


async def test_protected_reply_wraps_itself_in_interrupt_patches() -> None:
    """Rule 6: protection means interrupt_response=false before, true after —
    and always out-of-band, which the forbidden-frames test already pins."""
    async with MockRealtimeServer(caps=caps_mod.S2S) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            await linkobj.request_reply(link.ReplySpec(instructions="谢 SC", protected=True))
            done = await _next_event(linkobj.events(), link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            await linkobj.end_protection()
        finally:
            await linkobj.aclose()
        patches = [
            (e.get("session") or {}).get("turn_detection", {}).get("interrupt_response")
            for e in server.recorded.events
            if e.get("type") == "session.update" and "turn_detection" in (e.get("session") or {})
        ]
        assert patches == [False, True]


async def test_a_failed_protected_send_re_arms_barge_in_when_the_socket_lives() -> None:
    """Ledger #46, the recoverable half: a refusal on a healthy socket.

    Whoever disarms barge-in owns re-arming it, including on the way out
    through an exception — otherwise the session sits at
    interrupt_response=false for the rest of the stream and the streamer
    cannot get a word in.
    """
    async with MockRealtimeServer(caps=caps_mod.S2S) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            sent: list[dict[str, Any]] = []
            original = linkobj._client._send_raw

            async def record(frame: dict[str, Any]) -> None:
                sent.append(frame)
                await original(frame)

            linkobj._client._send_raw = record  # type: ignore[method-assign]

            async def refuse(frame: dict[str, Any]) -> link.ReplyHandle:
                raise RuntimeError("服务端拒了这条")

            linkobj._client.request_reply = refuse  # type: ignore[method-assign]

            with pytest.raises(RuntimeError):
                await linkobj.request_reply(link.ReplySpec(instructions="谢 SC", protected=True))
            assert _interrupt_flags(sent) == [False, True], "socket 还活着，打断该当场开回来"
        finally:
            await linkobj.aclose()


async def test_a_dead_socket_re_arms_barge_in_on_the_next_one() -> None:
    """Ledger #46, the half the first fix could not reach.

    request_reply raises from exactly one place — _send_raw — and
    end_protection reaches the wire through that same _send_raw. So the only
    condition that triggers an on-the-spot re-arm is the condition under which
    the re-arm cannot be sent. Re-arming immediately is right when the socket
    survives and useless precisely when it does not, which is the case the
    ledger described: one lost frame, and the streamer cannot interrupt for
    the rest of the stream.

    So the disarm is remembered. Whatever the server does with the old
    session's state, the socket that replaces it is told explicitly.
    """
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.S2S) as server:
        linkobj = S2SLink(server.url, clock=clock, auto_reconnect=True)
        await linkobj.connect()
        try:
            events = linkobj.events()
            calls = {"n": 0}
            original = linkobj._client._send_raw

            async def die_after_the_patch(frame: dict[str, Any]) -> None:
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise ConnectionError("写不出去了")
                await original(frame)

            linkobj._client._send_raw = die_after_the_patch  # type: ignore[method-assign]

            with pytest.raises(ConnectionError):
                await linkobj.request_reply(link.ReplySpec(instructions="谢 SC", protected=True))
            linkobj._client._send_raw = original  # type: ignore[method-assign]

            await server.drop_connection()
            down = await _next_event(events, link.LinkDown)
            assert isinstance(down, link.LinkDown)
            await clock.advance(1.5)
            up = await _next_event(events, link.LinkUp)
            assert isinstance(up, link.LinkUp)

            # The session's final barge-in state is what matters, and the
            # server reads frames on its own schedule, so ask the whole
            # transcript rather than a slice of it.
            flags = _interrupt_flags(server.recorded.events)
            assert (
                flags and flags[-1] is True
            ), f"会话最后停在关闭打断上，这一场主播插不进话：{flags}"
        finally:
            await linkobj.aclose()


def _interrupt_flags(frames: list[dict[str, Any]]) -> list[bool]:
    """The interrupt_response values that actually reached the wire, in order."""
    return [
        (f.get("session") or {})["turn_detection"]["interrupt_response"]
        for f in frames
        if f.get("type") == "session.update"
        and "interrupt_response" in (f.get("session") or {}).get("turn_detection", {})
    ]


# ------------------------------------------------------------ the handshake


async def test_a_refused_handshake_names_the_servers_reason() -> None:
    """A full server's one error frame must survive into the exception text.

    「服务端第一帧不是 session.created：error」 tells the operator nothing:
    error.type says WHICH refusal this is and error.message says why, and both
    die with the 1008 close unless connect() carries them out. Hit live when a
    stale client held the single s2s session slot (2026-08-14).
    """
    async with MockRealtimeServer(script=Script(faults={Fault.SESSION_LIMIT})) as server:
        linkobj = S2SLink(server.url)
        try:
            with pytest.raises(ConnectionError) as excinfo:
                await linkobj.connect()
        finally:
            await linkobj.aclose()
        assert "session_limit_reached" in str(excinfo.value)
        assert "session slots are in use" in str(excinfo.value)


@asynccontextmanager
async def _greeting_server(greeting: str) -> AsyncIterator[str]:
    """A socket that says one thing and then listens.

    MockRealtimeServer cannot play this part: it always opens with a
    well-formed session.created, which is exactly the assumption under test.
    """

    async def handle(conn: ServerConnection) -> None:
        await conn.send(greeting)
        with contextlib.suppress(websockets.ConnectionClosed):
            async for _ in conn:
                pass

    server = await serve(handle, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}/v1/realtime"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("greeting", "why"),
    [
        ("hello", "一个纯文本问候，根本不是 JSON"),
        ("[1, 2, 3]", "合法 JSON，但是数组不是对象"),
        ("null", "合法 JSON 的 null"),
    ],
    ids=["plain-text", "json-array", "json-null"],
)
async def test_a_socket_that_is_not_realtime_at_all_is_refused_in_chinese(
    greeting: str, why: str
) -> None:
    """Wrong port, an echo server, a dev server's hot-reload socket: none of
    them greet with a JSON object. Without the guard the streamer gets a
    JSONDecodeError traceback (or normalize()'s AttributeError on a list)
    instead of a sentence naming what answered.
    """
    async with _greeting_server(greeting) as url:
        linkobj = S2SLink(url)
        try:
            with pytest.raises(ConnectionError) as excinfo:
                await linkobj.connect()
        finally:
            await linkobj.aclose()
        assert "not_realtime" in str(excinfo.value), why
        # The greeting itself rides along: it is the only clue to what is
        # listening on that port.
        assert greeting[:20] in str(excinfo.value), why


async def test_a_refused_handshake_leaks_no_socket() -> None:
    """A caller that catches and retries must not stack up half-open sockets:
    connect() closes before it raises."""
    async with _greeting_server("hello") as url:
        linkobj = S2SLink(url)
        try:
            with pytest.raises(ConnectionError):
                await linkobj.connect()
            assert linkobj._client._ws is None
        finally:
            await linkobj.aclose()


# ------------------------------------------------------------ the log


async def test_a_reply_leaves_a_full_lifecycle_in_the_log() -> None:
    """The panel's log page has to answer "did the reply ever start speaking".

    Four debug lines per reply — requested, created, first frame, done — and
    exactly ONE first-frame line no matter how many deltas arrive. Deltas run at
    the audio cadence, so a line per frame is the one shape that must never
    appear here.
    """
    with _capturing("bilisama.realtime.client") as records:
        async with MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(delta_chunks=6, delta_interval_s=0.01)
        ) as server:
            linkobj = S2SLink(server.url)
            await linkobj.connect()
            try:
                await linkobj.request_reply(link.ReplySpec(instructions="打个招呼"))
                done = await _next_event(linkobj.events(), link.ReplyDone)
                assert isinstance(done, link.ReplyDone)
            finally:
                await linkobj.aclose()

    assert len(_named(records, "link.reply_requested")) == 1
    created = _named(records, "link.reply_created")
    assert len(created) == 1
    assert created[0]["ours"] is True, "our own create must not read as the streamer's VAD turn"

    first = _named(records, "link.reply_first_frame")
    assert len(first) == 1, f"one line per reply, not one per delta: {first}"
    assert first[0]["frame_kind"] == "text"
    assert first[0]["rid"] == created[0]["rid"]
    assert first[0]["first_frame_ms"] >= 0

    settled = _named(records, "link.reply_done")
    assert len(settled) == 1
    assert settled[0]["status"] == "completed"
    assert settled[0]["reply_chars"] == len(server.script.reply_text)
    assert settled[0]["handle_id"] == created[0]["handle_id"]


async def test_the_slot_flip_is_logged_once_each_way() -> None:
    """Taking and freeing the single slot is a state flip, so it sits at info.

    Once each per reply, not once per caller that touches the event: the create
    takes it and the response.created takes it again, and _on_disconnect frees
    it once per open record. A line per call would put four lines on the
    streamer's default filter for one thing happening.
    """
    with _capturing("bilisama.realtime.client") as records:
        async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=2)) as server:
            linkobj = S2SLink(server.url)
            await linkobj.connect()
            try:
                await linkobj.request_reply(link.ReplySpec(instructions="一"))
                await _next_event(linkobj.events(), link.ReplyDone)
            finally:
                await linkobj.aclose()

    taken = _named(records, "link.slot_taken")
    freed = _named(records, "link.slot_freed")
    assert len(taken) == 1, taken
    assert taken[0]["reason"] == "request"
    assert len(freed) == 1, freed
    assert freed[0]["reason"] == "reply_done"
    # An uncontended wait is every session.update and every create on a quiet
    # link. Only the queue that actually formed is news.
    assert _named(records, "link.slot_waited") == []


async def test_a_queued_request_says_how_long_it_waited() -> None:
    """Rule 5 serialises two creates. The second one's delay is invisible from
    outside — no failure, no event — so the wait itself has to say so, or "the
    SC thank-you came a beat late" has no answer anywhere."""
    with _capturing("bilisama.realtime.client") as records:
        async with MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(delta_chunks=3, delta_interval_s=0.02)
        ) as server:
            linkobj = S2SLink(server.url)
            await linkobj.connect()
            try:
                first = asyncio.create_task(
                    linkobj.request_reply(link.ReplySpec(instructions="一"))
                )
                second = asyncio.create_task(
                    linkobj.request_reply(link.ReplySpec(instructions="二"))
                )
                events = linkobj.events()
                await _next_event(events, link.ReplyDone)
                await _next_event(events, link.ReplyDone, timeout=10.0)
                await asyncio.gather(first, second)
            finally:
                await linkobj.aclose()

    waited = _named(records, "link.slot_waited")
    assert waited, "the second create queued behind the first and said nothing"
    assert waited[0]["purpose"] == "reply"
    assert waited[0]["waited_ms"] >= 0


async def test_cancelling_one_handle_twice_sends_one_cancel() -> None:
    """A bare response.cancel names no reply: it kills whoever holds the slot.
    Two callers cancelling the same reply (the voice gate and the scheduler's
    guard can both decide to) must therefore cost one frame, not two — the
    second frame would land after the done and murder the next reply.

    The two cancels are gathered rather than awaited in turn: the second must
    short-circuit on the record's own bookkeeping, before any done frame from
    the server could have settled the record and made the test trivially pass.
    """
    script = Script(delta_chunks=4, delta_interval_s=0.05)
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            handle = await linkobj.request_reply(link.ReplySpec(instructions="讲个长故事"))
            events = linkobj.events()
            await _next_event(events, link.ReplyTextDelta)
            await asyncio.gather(linkobj.cancel(handle), linkobj.cancel(handle))
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.CANCELLED
            assert server.recorded.count("response.cancel") == 1
        finally:
            await linkobj.aclose()


async def test_a_cancel_that_found_nothing_is_not_the_streamer_s_problem() -> None:
    """The one error the client swallows, and why it may.

    The voice gate mutes a turn the moment its tag closes, then keeps decoding
    for up to 300 ms to catch the note before it cancels. That window gives the
    reply time to finish on its own, so the cancel often lands on a response
    that is already over and DashScope answers `invalid_request_error`. Nothing
    is wrong and nothing is actionable — the turn was silenced either way — but
    it used to reach the streamer's chat as a line of red English, on 20% of
    skips in production (3% before the window existed).

    Narrow on purpose: only an error naming a missing response, and only while
    a cancel we sent is still in flight. Anything else still comes up.
    """
    script = Script(delta_chunks=30, delta_interval_s=0.05)
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            events = linkobj.events()
            handle = await linkobj.request_reply(link.ReplySpec(instructions="讲个长故事"))
            await _next_event(events, link.ReplyTextDelta)
            await linkobj.cancel(handle)
            await asyncio.sleep(0.1)  # let the frame land before reading the log
            assert server.recorded.count("response.cancel") == 1
            # What DashScope answers when the reply beat the cancel there.
            await server.send(
                dia.ServerEvent.ERROR,
                error={
                    "type": "invalid_request_error",
                    "message": "Conversation has no active response.",
                },
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_next_event(events, link.LinkError), timeout=0.6)
        finally:
            await linkobj.aclose()


async def test_an_unprompted_invalid_request_still_reaches_the_streamer() -> None:
    """The suppression is scoped to a cancel we just sent, not to the code.

    Same error text, no cancel in flight: it must come up, or a real rejected
    request would go missing for the same reason the benign one does.
    """
    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        try:
            events = linkobj.events()
            await server.send(
                dia.ServerEvent.ERROR,
                error={
                    "type": "invalid_request_error",
                    "message": "Conversation has no active response.",
                },
            )
            err = await asyncio.wait_for(_next_event(events, link.LinkError), timeout=2.0)
            assert isinstance(err, link.LinkError)
            assert err.code == "invalid_request_error"
        finally:
            await linkobj.aclose()
