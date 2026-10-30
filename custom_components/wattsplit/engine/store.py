"""SQLite log of edges, state events and toggles.

The log, not the raw samples, is the learning substrate: it is orders of
magnitude smaller than the sample stream, outlives the recorder's retention,
and is enough to replay the engine with different parameters.

``sqlite3`` blocks. Call from an executor in Home Assistant.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import Edge, Toggle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    ts REAL NOT NULL,
    seen_ts REAL NOT NULL,
    delta_w REAL NOT NULL,
    device_id TEXT,
    parts TEXT
);
CREATE INDEX IF NOT EXISTS edges_seen ON edges (seen_ts);
CREATE TABLE IF NOT EXISTS state_events (
    ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    is_on INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS state_ts ON state_events (ts);
CREATE TABLE IF NOT EXISTS toggles (
    ts REAL NOT NULL,
    seen_ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    is_on INTEGER NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS toggles_seen ON toggles (seen_ts);
CREATE TABLE IF NOT EXISTS counter_samples (
    ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    value_wh REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS counter_ts ON counter_samples (ts);
"""


class EdgeLog:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(edges)")}
            if "parts" not in columns:  # log written by an earlier version
                self._db.execute("ALTER TABLE edges ADD COLUMN parts TEXT")

    def add_edge(self, edge: Edge, device_id: str | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                (
                    edge.ts,
                    edge.seen_ts or edge.ts,
                    edge.delta_w,
                    device_id,
                    json.dumps(edge.parts),
                ),
            )

    def add_state(self, ts: float, device_id: str, is_on: bool) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO state_events VALUES (?, ?, ?)", (ts, device_id, is_on)
            )

    def add_toggle(self, toggle: Toggle) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO toggles VALUES (?, ?, ?, ?, ?, ?)",
                (
                    toggle.ts,
                    toggle.seen_ts,
                    toggle.device_id,
                    toggle.is_on,
                    json.dumps(toggle.before),
                    json.dumps(toggle.after),
                ),
            )

    def add_counter(self, ts: float, device_id: str, value_wh: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO counter_samples VALUES (?, ?, ?)",
                (ts, device_id, value_wh),
            )

    def counters(self, since: float = 0.0) -> list[tuple[float, str, float]]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, device_id, value_wh FROM counter_samples "
                "WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()

    def edges(
        self, since: float = 0.0, unexplained_only: bool = False
    ) -> list[tuple[Edge, str | None]]:
        sql = (
            "SELECT ts, seen_ts, delta_w, device_id, parts FROM edges "
            "WHERE seen_ts >= ?"
        )
        if unexplained_only:
            sql += " AND device_id IS NULL"
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seen_ts", (since,)).fetchall()
        return [
            (
                Edge(ts, delta, seen, tuple((c, d) for c, d in json.loads(parts or "[]"))),
                dev,
            )
            for ts, seen, delta, dev, parts in rows
        ]

    def state_events(self, since: float = 0.0) -> list[tuple[float, str, bool]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, device_id, is_on FROM state_events WHERE ts >= ? "
                "ORDER BY ts",
                (since,),
            ).fetchall()
        return [(ts, dev, bool(on)) for ts, dev, on in rows]

    def toggles(self, since: float = 0.0) -> list[Toggle]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, seen_ts, device_id, is_on, before, after FROM toggles "
                "WHERE seen_ts >= ? ORDER BY seen_ts",
                (since,),
            ).fetchall()
        return [
            Toggle(
                dev,
                ts,
                bool(on),
                tuple((c, v) for c, v in json.loads(before)),
                tuple((c, v) for c, v in json.loads(after)),
                seen,
            )
            for ts, seen, dev, on, before, after in rows
        ]

    def prune(self, before_ts: float) -> int:
        """Delete everything older than ``before_ts``; return rows removed."""
        with self._lock, self._db:
            n = self._db.execute(
                "DELETE FROM edges WHERE seen_ts < ?", (before_ts,)
            ).rowcount
            n += self._db.execute(
                "DELETE FROM state_events WHERE ts < ?", (before_ts,)
            ).rowcount
            n += self._db.execute(
                "DELETE FROM toggles WHERE seen_ts < ?", (before_ts,)
            ).rowcount
            n += self._db.execute(
                "DELETE FROM counter_samples WHERE ts < ?", (before_ts,)
            ).rowcount
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()
