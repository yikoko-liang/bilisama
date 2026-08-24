"""What dev-talk prints, and in what order.

The terminal transcript is the only record of a live session a developer
actually reads, and it had `_consume_events` printing link events in arrival
order — which is not causal order. Speech recognition runs beside generation
rather than before it, so the line saying what the streamer said can land
after the reply answering it, and the log reads as if she answered first.

Nothing could have caught this: the mock server does not model
`conversation.item.input_audio_transcription.*` at all, so in every test the
transcript that never arrives can never arrive late.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from bilisama.dev_talk import _consume_events
from bilisama.realtime import link
from bilisama.realtime.link import ReplyStatus

_HANDLE = link.ReplyHandle(handle_id=1)


def _turn(said: str, *, transcript: str, late: bool) -> list[link.LinkEvent]:
    """One user turn. `late` puts the recogniser behind the reply, which is the
    race this file exists for — both orders happen against a real endpoint."""
    heard = link.UserTranscriptDone(transcript)
    reply = link.ReplyTextDelta(_HANDLE, said)
    body: Sequence[link.LinkEvent] = (reply, heard) if late else (heard, reply)
    return [
        link.SpeechStopped(audio_ms=800),
        *body,
        link.ReplyDone(_HANDLE, ReplyStatus.COMPLETED, said),
    ]


async def _feed(events: Sequence[link.LinkEvent]) -> AsyncIterator[link.LinkEvent]:
    for event in events:
        yield event
        await asyncio.sleep(0)


def _run(events: Sequence[link.LinkEvent], capsys: pytest.CaptureFixture[str]) -> list[str]:
    asyncio.run(_consume_events(_feed(events), None, None, stream_text=False))
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def _order(lines: Sequence[str], said: str, transcript: str) -> tuple[int, int]:
    heard_at = next(i for i, line in enumerate(lines) if transcript in line)
    said_at = next(i for i, line in enumerate(lines) if said in line)
    return heard_at, said_at


def test_a_late_transcript_still_prints_before_the_reply_it_explains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported bug, in the order it was seen on screen.

    The recogniser finished after the reply text had already gone out, so the
    log read: her answer, then the question. A developer reading that cannot
    tell what caused what — and the first thing they suspect is the thing that
    is working.
    """
    # Punctuated on purpose: the buffered printer flushes on a sentence end
    # (_SENTENCE_END), so a reply that finishes a sentence goes out the moment
    # it arrives — which is what put it ahead of the transcript on screen.
    events = [
        *_turn("先来一句。", transcript="第一句", late=False),
        *_turn("这得看你在哪座城市啊，我这 AI 没长眼睛。", transcript="今天天气怎么样", late=True),
    ]
    lines = _run(events, capsys)
    heard_at, said_at = _order(lines, "这得看你在哪座城市啊", "今天天气怎么样")
    assert heard_at < said_at, "回复排在了它要回答的那句话前面：\n" + "\n".join(lines)


def test_a_transcript_that_arrives_first_is_not_delayed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The common case must stay exactly as it was: no holding, no reordering."""
    events = [
        *_turn("先来一句。", transcript="第一句", late=False),
        *_turn("我这儿没窗户，看不着天。", transcript="那个啥", late=False),
    ]
    lines = _run(events, capsys)
    heard_at, said_at = _order(lines, "我这儿没窗户", "那个啥")
    assert heard_at < said_at


def test_a_provider_that_never_transcribes_streams_its_reply_as_before(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """s2s with `--stt none` sends no transcript, ever.

    Waiting for one there would hold every reply until it ended, which is the
    regression the sentence-at-a-time printing was built to avoid. So the wait
    only arms once this provider has been seen to transcribe at least once.
    """
    events: list[link.LinkEvent] = [
        link.SpeechStopped(audio_ms=800),
        link.ReplyTextDelta(_HANDLE, "她说的话。"),
        link.ReplyDone(_HANDLE, ReplyStatus.COMPLETED, "她说的话。"),
    ]
    printed: list[str] = []

    async def watch() -> None:
        async def feed() -> AsyncIterator[link.LinkEvent]:
            yield events[0]
            await asyncio.sleep(0)
            yield events[1]
            await asyncio.sleep(0)
            # Read the buffer with the reply still open: held text would not be
            # here yet, and that is the whole difference being asserted.
            printed.append(capsys.readouterr().out)
            yield events[2]

        await _consume_events(feed(), None, None, stream_text=False)

    asyncio.run(watch())
    assert "她说的话。" in printed[0], "没人会来的转写把这段回复卡住了"
