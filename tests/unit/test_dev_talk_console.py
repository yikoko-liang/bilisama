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
    # The FIRST turn of the connection, which is the hard one: nothing has told
    # us yet whether this provider transcribes at all.
    #
    # Punctuated on purpose: the buffered printer flushes on a sentence end
    # (_SENTENCE_END), so a reply that finishes a sentence goes out the moment
    # it arrives — which is what put it ahead of the transcript on screen.
    events = _turn(
        "这得看你在哪座城市啊，我这 AI 没长眼睛。", transcript="今天天气怎么样", late=True
    )
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


def test_a_provider_that_never_transcribes_is_made_to_wait_exactly_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """s2s with `--stt none` sends no transcript, ever.

    Nothing announces that up front — no session field asks for transcription
    and the client does not read session.updated — so the first turn waits and
    finds out. That costs one turn of buffering per connection. What must not
    happen is paying it twice: waiting on every turn would hold each reply to
    its end, which is the lag the sentence-at-a-time printing exists to avoid.
    """
    seen: list[str] = []
    handle = link.ReplyHandle(handle_id=1)

    async def feed() -> AsyncIterator[link.LinkEvent]:
        for turn in ("第一轮的回复。", "第二轮的回复。"):
            yield link.SpeechStopped(audio_ms=800)
            await asyncio.sleep(0)
            yield link.ReplyTextDelta(handle, turn)
            await asyncio.sleep(0)
            # Sampled with the reply still open: held text is not here yet.
            seen.append(capsys.readouterr().out)
            yield link.ReplyDone(handle, ReplyStatus.COMPLETED, turn)
            await asyncio.sleep(0)

    asyncio.run(_consume_events(feed(), None, None, stream_text=False))
    assert "第一轮的回复。" not in seen[0], "第一轮没等，转写迟到的 provider 第一轮就还是反的"
    assert "第二轮的回复。" in seen[1], "问过一次已经知道它不转写，不该再等第二次"
