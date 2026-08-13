"""Tier 0 memory: counters and rows, no LLM anywhere.

"阿强又来了，这是第五次" is one indexed SELECT — this layer alone carries the
whole regular-viewer feeling (plan section 4.7). Writes are synchronous on the
event loop on purpose: a local SQLite insert is microseconds, and the plan
pins Tier 0 as the synchronous tier.

`write_batch_ms > 0` switches the event path to write-behind for flood-rate
rooms (plan section 16.8 item 26): rows buffer in memory and land as ONE
transaction when the window ages out or the batch fills. Every read flushes
first, so read-after-write semantics are identical either way; a crash can
lose at most one buffered window of event rows, which are 7-day disposable.

Time only ever comes from the injected clock. Events are pruned after a
retention window; viewer rows never are.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bilisama.ingest.events import LiveEvent

if TYPE_CHECKING:
    from bilisama.clock import Clock

__all__ = ["STREAM_TZ", "FactRow", "MemoryStore", "ViewerRow", "logical_date"]

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Streams routinely run past midnight; a 04:00 boundary keeps "tonight" one
# logical day (plan section 4.7).
_DAY_BOUNDARY_H = 4

# The product speaks to Bilibili streamers: stream-world arithmetic runs on
# China time, pinned rather than host-local so the same DB reads the same on
# any machine. Rows still store UTC (schema.sql).
STREAM_TZ = timezone(timedelta(hours=8))

# Write-behind flushes at whichever comes first: the configured window, or
# this many buffered events — one transaction either way.
_BATCH_MAX_ROWS = 200

_EVENT_INSERT = (
    "INSERT INTO event (stream_id, kind, identity, uname, text, value_cny, wall_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# UPSERT keeps a viewer one round trip; streams_seen bumps only when the
# viewer's last_stream_id is stale, so N events in one stream count 1.
_VIEWER_UPSERT = """
    INSERT INTO viewer (identity, uid, uid_hash, uname, guard_level,
                        first_seen, last_seen, streams_seen, last_stream_id,
                        msg_count, gift_value_cny)
    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    ON CONFLICT(identity) DO UPDATE SET
        uname = COALESCE(NULLIF(excluded.uname, ''), uname),
        guard_level = excluded.guard_level,
        last_seen = excluded.last_seen,
        streams_seen = streams_seen
            + (last_stream_id != excluded.last_stream_id),
        last_stream_id = excluded.last_stream_id,
        msg_count = msg_count + excluded.msg_count,
        gift_value_cny = gift_value_cny + excluded.gift_value_cny
    """


def logical_date(wall: datetime) -> datetime:
    """The stream-world date: the calendar day flips at 04:00 CHINA time.

    Computing the boundary on raw UTC put it at noon Beijing (B4): every
    morning stream landed on yesterday and "本周第 N 场" flipped at lunch.
    """
    return wall.astimezone(STREAM_TZ) - timedelta(hours=_DAY_BOUNDARY_H)


@dataclass(frozen=True, slots=True)
class ViewerRow:
    identity: str
    uid: int
    uname: str
    guard_level: str
    first_seen: str
    last_seen: str
    streams_seen: int
    msg_count: int
    gift_value_cny: float


@dataclass(frozen=True, slots=True)
class FactRow:
    scope: str
    subject: str
    text: str
    tags: str


class MemoryStore:
    """One room, one file. `:memory:` works for tests."""

    def __init__(self, db_path: Path | str, clock: Clock, *, write_batch_ms: int = 0) -> None:
        self._clock = clock
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._stream_id = 0
        self._batch_s = write_batch_ms / 1000.0
        self._pending_events: list[tuple[object, ...]] = []
        self._pending_viewers: list[tuple[object, ...]] = []
        self._pending_since = 0.0

    def close(self) -> None:
        self._flush_pending()
        self._db.close()

    def _flush_pending(self) -> None:
        """Land every buffered row in one transaction. No-op when empty.

        Called by every reader before it looks, and at stream boundaries —
        that discipline is what keeps write-behind invisible to callers."""
        if not self._pending_events:
            return
        with self._db:
            self._db.executemany(_EVENT_INSERT, self._pending_events)
            # executemany applies rows in order, so the streams_seen bump
            # logic sees the same sequence a per-event write would.
            self._db.executemany(_VIEWER_UPSERT, self._pending_viewers)
        self._pending_events.clear()
        self._pending_viewers.clear()

    # ------------------------------------------------------------ streams

    def begin_stream(self) -> int:
        """Open a stream row; every event lands in it until end_stream()."""
        self._flush_pending()
        cur = self._db.execute(
            "INSERT INTO stream (started_at) VALUES (?)",
            (self._clock.wall().isoformat(),),
        )
        self._db.commit()
        self._stream_id = int(cur.lastrowid or 0)
        return self._stream_id

    def end_stream(self) -> None:
        if not self._stream_id:
            return
        self._flush_pending()
        self._db.execute(
            "UPDATE stream SET ended_at = ? WHERE id = ?",
            (self._clock.wall().isoformat(), self._stream_id),
        )
        self._db.commit()
        self._stream_id = 0

    @property
    def stream_id(self) -> int:
        return self._stream_id

    def stream_started_at(self) -> datetime | None:
        row = self._db.execute(
            "SELECT started_at FROM stream WHERE id = ?", (self._stream_id,)
        ).fetchone()
        return datetime.fromisoformat(row["started_at"]) if row else None

    def streams_this_week(self) -> int:
        """How many streams started in the current logical ISO week, this one
        included — the "本周第 N 场" number."""
        this_week = logical_date(self._clock.wall()).isocalendar()[:2]
        rows = self._db.execute("SELECT started_at FROM stream").fetchall()
        return sum(
            1
            for row in rows
            if logical_date(datetime.fromisoformat(row["started_at"])).isocalendar()[:2]
            == this_week
        )

    # ------------------------------------------------------------ tier 0

    def on_event(self, event: LiveEvent) -> None:
        """Record one event and bump its viewer. Runs for every event, speak
        switch or not — "not speaking" is not "not knowing" (section 2.7)."""
        if not self._stream_id:
            raise RuntimeError("begin_stream() 还没调用，事件不知道该记到哪一场")
        now = self._clock.wall().isoformat()
        viewer = event.viewer
        event_row = (
            self._stream_id,
            event.kind.value,
            viewer.identity,
            viewer.name,
            event.text,
            event.value_cny,
            now,
        )
        viewer_row = (
            viewer.identity,
            viewer.uid,
            viewer.uid_hash,
            viewer.name,
            viewer.guard_level.value,
            now,
            now,
            self._stream_id,
            1 if event.text else 0,
            event.value_cny,
        )
        # Identity-less events (ROOM_STATE and friends) record the event row
        # only: an "anon" viewer row would accumulate streams_seen every
        # stream and float to the top of the regulars prompt segment.
        track_viewer = viewer.identity != "anon"
        if self._batch_s <= 0:
            self._db.execute(_EVENT_INSERT, event_row)
            if track_viewer:
                self._db.execute(_VIEWER_UPSERT, viewer_row)
            self._db.commit()
            return
        if not self._pending_events:
            self._pending_since = self._clock.monotonic()
        self._pending_events.append(event_row)
        if track_viewer:
            self._pending_viewers.append(viewer_row)
        aged = self._clock.monotonic() - self._pending_since >= self._batch_s
        if aged or len(self._pending_events) >= _BATCH_MAX_ROWS:
            self._flush_pending()

    def viewer(self, identity: str) -> ViewerRow | None:
        self._flush_pending()
        row = self._db.execute("SELECT * FROM viewer WHERE identity = ?", (identity,)).fetchone()
        if row is None:
            return None
        return self._viewer_row(row)

    def present_regulars(self, *, limit: int = 5) -> list[ViewerRow]:
        """Viewers active this stream who have been here before, most loyal
        first. Feeds the 在场常客 prompt segment."""
        self._flush_pending()
        rows = self._db.execute(
            """
            SELECT * FROM viewer
            WHERE last_stream_id = ? AND streams_seen >= 2
            ORDER BY streams_seen DESC, gift_value_cny DESC
            LIMIT ?
            """,
            (self._stream_id, limit),
        ).fetchall()
        return [self._viewer_row(row) for row in rows]

    def top_viewers(self, *, limit: int = 20) -> list[ViewerRow]:
        """This stream's most engaged viewers — the distiller's shortlist."""
        self._flush_pending()
        rows = self._db.execute(
            """
            SELECT * FROM viewer
            WHERE last_stream_id = ?
            ORDER BY gift_value_cny DESC, msg_count DESC
            LIMIT ?
            """,
            (self._stream_id, limit),
        ).fetchall()
        return [self._viewer_row(row) for row in rows]

    @staticmethod
    def _viewer_row(row: sqlite3.Row) -> ViewerRow:
        return ViewerRow(
            identity=row["identity"],
            uid=row["uid"],
            uname=row["uname"],
            guard_level=row["guard_level"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            streams_seen=row["streams_seen"],
            msg_count=row["msg_count"],
            gift_value_cny=row["gift_value_cny"],
        )

    def recent_events(self, *, limit: int = 30) -> list[str]:
        """The newest event lines of this stream, oldest first — distiller and
        proactive-loop input."""
        self._flush_pending()
        rows = self._db.execute(
            """
            SELECT kind, uname, text, value_cny FROM event
            WHERE stream_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (self._stream_id, limit),
        ).fetchall()
        lines = []
        for row in reversed(rows):
            name = row["uname"] or "观众"
            if row["text"]:
                lines.append(f"[{row['kind']}] {name}: {row['text']}")
            else:
                lines.append(f"[{row['kind']}] {name}")
        return lines

    def prune_events(self, *, retain_days: int) -> int:
        """Drop event rows past retention. Viewer rows are never touched."""
        self._flush_pending()
        cutoff = (self._clock.wall() - timedelta(days=retain_days)).isoformat()
        cur = self._db.execute("DELETE FROM event WHERE wall_at < ?", (cutoff,))
        self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ facts

    def facts(self, scope: str, subject: str = "") -> list[FactRow]:
        rows = self._db.execute(
            "SELECT scope, subject, text, tags FROM fact WHERE scope = ? AND subject = ?"
            " ORDER BY id",
            (scope, subject),
        ).fetchall()
        return [
            FactRow(scope=r["scope"], subject=r["subject"], text=r["text"], tags=r["tags"])
            for r in rows
        ]

    def replace_facts(self, scope: str, subject: str, facts: list[tuple[str, str]]) -> None:
        """Delete-then-insert on (scope, subject) — openhanako's replacement
        semantics with our key, so the table stays bounded by construction."""
        now = self._clock.wall().isoformat()
        with self._db:
            self._db.execute(
                "DELETE FROM fact WHERE scope = ? AND subject = ?",
                (scope, subject),
            )
            self._db.executemany(
                "INSERT INTO fact (scope, subject, text, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                [(scope, subject, text, tags, now) for text, tags in facts],
            )
