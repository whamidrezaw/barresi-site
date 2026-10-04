"""SQLite state: page snapshots, seen items, per-watch health and the notification outbox.

Every detected change is written as an event in the *same transaction* as the
new snapshot (outbox pattern). Events stay "pending" until Telegram confirms
delivery, so a network outage or a crash can delay a notification but never
lose it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    watch_id      TEXT NOT NULL,
    url           TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'page',
    digest        TEXT,
    snapshot      TEXT,
    title         TEXT,
    engine        TEXT,
    fingerprint   TEXT,
    etag          TEXT,
    last_modified TEXT,
    size          INTEGER,
    first_seen    REAL NOT NULL,
    last_seen     REAL,
    last_changed  REAL,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (watch_id, url)
);
CREATE TABLE IF NOT EXISTS seen_items (
    watch_id   TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (watch_id, item_id)
);
CREATE TABLE IF NOT EXISTS watch_state (
    watch_id           TEXT PRIMARY KEY,
    engine             TEXT,
    fingerprint        TEXT,
    crawl_fingerprint  TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    first_error_at     REAL,
    last_error         TEXT,
    alerted            INTEGER NOT NULL DEFAULT 0,
    last_ok            REAL,
    last_check         REAL,
    last_crawl         REAL,
    last_notified      REAL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id     TEXT NOT NULL,
    url          TEXT,
    kind         TEXT NOT NULL,
    created      REAL NOT NULL,
    summary      TEXT,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    progress     TEXT,
    sent_at      REAL,
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS events_pending ON events(status, next_attempt);
CREATE INDEX IF NOT EXISTS events_watch ON events(watch_id, created);
"""

_WATCH_COLUMNS = {
    "engine", "fingerprint", "crawl_fingerprint", "consecutive_errors", "first_error_at", "last_error",
    "alerted", "last_ok", "last_check", "last_crawl", "last_notified",
}

Event = tuple  # (kind, summary, payload: dict, status)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ events

    def _insert_event(self, watch_id: str, url: str | None, event: Event) -> int:
        kind, summary, payload, status = event
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO events (watch_id, url, kind, created, summary, payload, status, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (watch_id, url, kind, now, summary, json.dumps(payload, ensure_ascii=False), status,
             None if status == "pending" else now),
        )
        return cur.lastrowid

    def add_event(self, watch_id: str, url: str | None, kind: str, summary: str, payload: dict,
                  status: str = "pending") -> int:
        with self.conn:
            return self._insert_event(watch_id, url, (kind, summary, payload, status))

    def pending_events(self, now: float | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE status = 'pending' AND next_attempt <= ? ORDER BY id",
            (now if now is not None else time.time(),),
        ).fetchall()

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE status = 'pending'").fetchone()[0]

    def set_event_progress(self, event_id: int, progress: list[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE events SET progress = ? WHERE id = ?", (json.dumps(progress), event_id))

    def mark_events_sent(self, event_ids: list[int]) -> None:
        now = time.time()
        with self.conn:
            self.conn.executemany(
                "UPDATE events SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
                [(now, event_id) for event_id in event_ids],
            )

    def finish_events(self, event_ids: list[int], status: str, error: str) -> None:
        """Close events that cannot (or need not) be retried: status 'sent' or 'failed'."""
        now = time.time()
        with self.conn:
            self.conn.executemany(
                "UPDATE events SET status = ?, sent_at = ?, last_error = ? WHERE id = ?",
                [(status, now, error[:500], event_id) for event_id in event_ids],
            )

    def mark_event_retry(self, event_id: int, attempts: int, next_attempt: float, error: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE events SET attempts = ?, next_attempt = ?, last_error = ? WHERE id = ?",
                (attempts, next_attempt, error[:500], event_id),
            )

    def recent_events(self, watch_id: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if watch_id:
            return self.conn.execute(
                "SELECT * FROM events WHERE watch_id = ? ORDER BY id DESC LIMIT ?", (watch_id, limit)
            ).fetchall()
        return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def event_counts_since(self, since: float) -> dict[str, Counter]:
        counts: dict[str, Counter] = {}
        for row in self.conn.execute(
            "SELECT watch_id, kind, COUNT(*) AS n FROM events WHERE created >= ? GROUP BY watch_id, kind", (since,)
        ):
            counts.setdefault(row["watch_id"], Counter())[row["kind"]] = row["n"]
        return counts

    # ------------------------------------------------------------------ pages

    def get_page(self, watch_id: str, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pages WHERE watch_id = ? AND url = ?", (watch_id, url)).fetchone()

    def page_rows(self, watch_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM pages WHERE watch_id = ?", (watch_id,)).fetchall()
        return {row["url"]: row for row in rows}

    def page_urls(self, watch_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT url FROM pages WHERE watch_id = ?", (watch_id,))}

    def record_page(self, watch_id: str, url: str, *, kind: str = "page", digest: str | None = None,
                    snapshot: str | None = None, title: str | None = None, engine: str | None = None,
                    fingerprint: str | None = None, etag: str | None = None, last_modified: str | None = None,
                    size: int | None = None, changed: bool = False, event: Event | None = None) -> None:
        """Upsert a page snapshot and (atomically) queue its change event."""
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pages (watch_id, url, kind, digest, snapshot, title, engine, fingerprint, etag,
                                   last_modified, size, first_seen, last_seen, last_changed, fail_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(watch_id, url) DO UPDATE SET
                    kind = excluded.kind, digest = excluded.digest, snapshot = excluded.snapshot,
                    title = excluded.title, engine = excluded.engine, fingerprint = excluded.fingerprint,
                    etag = excluded.etag, last_modified = excluded.last_modified, size = excluded.size,
                    last_seen = excluded.last_seen,
                    last_changed = CASE WHEN ? THEN excluded.last_seen ELSE pages.last_changed END,
                    fail_count = 0
                """,
                (watch_id, url, kind, digest, snapshot, title, engine, fingerprint, etag, last_modified, size,
                 now, now, now if changed else None, 1 if changed else 0),
            )
            if event is not None:
                self._insert_event(watch_id, url, event)

    def touch_page(self, watch_id: str, url: str, etag: str | None = None, last_modified: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET last_seen = ?, fail_count = 0, etag = COALESCE(?, etag), "
                "last_modified = COALESCE(?, last_modified) WHERE watch_id = ? AND url = ?",
                (time.time(), etag, last_modified, watch_id, url),
            )

    def page_failed(self, watch_id: str, url: str) -> int:
        with self.conn:
            self.conn.execute("UPDATE pages SET fail_count = fail_count + 1 WHERE watch_id = ? AND url = ?",
                              (watch_id, url))
        row = self.conn.execute("SELECT fail_count FROM pages WHERE watch_id = ? AND url = ?",
                                (watch_id, url)).fetchone()
        return row[0] if row else 0

    def delete_page(self, watch_id: str, url: str, event: Event | None = None) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM pages WHERE watch_id = ? AND url = ?", (watch_id, url))
            if event is not None:
                self._insert_event(watch_id, url, event)

    # ------------------------------------------------------------------ items

    def seen_ids(self, watch_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT item_id FROM seen_items WHERE watch_id = ?", (watch_id,))}

    def has_items(self, watch_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_items WHERE watch_id = ? LIMIT 1", (watch_id,)).fetchone() is not None

    def record_items(self, watch_id: str, item_ids: list[str], *, reset: bool = False, fingerprint: str | None = None,
                     event: Event | None = None) -> None:
        now = time.time()
        with self.conn:
            if reset:
                self.conn.execute("DELETE FROM seen_items WHERE watch_id = ?", (watch_id,))
            self.conn.executemany(
                "INSERT INTO seen_items (watch_id, item_id, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(watch_id, item_id) DO UPDATE SET last_seen = excluded.last_seen",
                [(watch_id, item_id, now, now) for item_id in item_ids],
            )
            if fingerprint is not None:
                self._upsert_watch(watch_id, {"fingerprint": fingerprint})
            if event is not None:
                self._insert_event(watch_id, None, event)

    # ------------------------------------------------------------------ watch state

    def watch_state(self, watch_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM watch_state WHERE watch_id = ?", (watch_id,)).fetchone()
        return dict(row) if row else {}

    def _upsert_watch(self, watch_id: str, fields: dict) -> None:
        unknown = set(fields) - _WATCH_COLUMNS
        if unknown:
            raise ValueError(f"unknown watch_state columns: {unknown}")
        self.conn.execute("INSERT OR IGNORE INTO watch_state (watch_id) VALUES (?)", (watch_id,))
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            self.conn.execute(f"UPDATE watch_state SET {assignments} WHERE watch_id = ?",
                              (*fields.values(), watch_id))

    def update_watch(self, watch_id: str, **fields) -> None:
        with self.conn:
            self._upsert_watch(watch_id, fields)

    def record_check_error(self, watch_id: str, message: str) -> dict:
        now = time.time()
        with self.conn:
            self._upsert_watch(watch_id, {})
            self.conn.execute(
                "UPDATE watch_state SET consecutive_errors = consecutive_errors + 1, last_error = ?, last_check = ?, "
                "first_error_at = COALESCE(first_error_at, ?) WHERE watch_id = ?",
                (message[:500], now, now, watch_id),
            )
        return self.watch_state(watch_id)

    def record_check_ok(self, watch_id: str) -> dict:
        """Mark a successful check; returns the state from *before* the reset."""
        previous = self.watch_state(watch_id)
        now = time.time()
        with self.conn:
            self._upsert_watch(watch_id, {"consecutive_errors": 0, "first_error_at": None, "alerted": 0,
                                          "last_ok": now, "last_check": now})
        return previous

    # ------------------------------------------------------------------ maintenance

    def reset_watch(self, watch_id: str) -> None:
        with self.conn:
            for table in ("pages", "seen_items", "watch_state"):
                self.conn.execute(f"DELETE FROM {table} WHERE watch_id = ?", (watch_id,))

    def forget_unknown_watches(self, known_ids: list[str]) -> list[str]:
        stored = {r[0] for r in self.conn.execute(
            "SELECT watch_id FROM pages UNION SELECT watch_id FROM seen_items UNION SELECT watch_id FROM watch_state")}
        stale = sorted(stored - set(known_ids))
        for watch_id in stale:
            self.reset_watch(watch_id)
        return stale

    def prune(self, history_days: int) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM events WHERE status IN ('sent', 'filtered') AND created < ?",
                              (now - history_days * 86400,))
            self.conn.execute("DELETE FROM seen_items WHERE last_seen < ?", (now - max(history_days, 60) * 86400,))
