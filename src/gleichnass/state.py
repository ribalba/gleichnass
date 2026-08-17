"""SQLite-backed memory of what has already been sent.

Without this the imminent rule would announce the same shower every time cron
fires. One row per (user, rule); a delivery log alongside it for debugging.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .rules import RuleState

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_state (
    user_id          TEXT NOT NULL,
    rule             TEXT NOT NULL,
    last_run_at      TEXT,
    last_notified_at TEXT,
    last_event_start TEXT,
    PRIMARY KEY (user_id, rule)
);
CREATE TABLE IF NOT EXISTS deliveries (
    sent_at  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    rule     TEXT NOT NULL,
    channel  TEXT NOT NULL,
    title    TEXT,
    body     TEXT,
    ok       INTEGER NOT NULL,
    error    TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_sent_at ON deliveries (sent_at);
CREATE TABLE IF NOT EXISTS places (
    query      TEXT PRIMARY KEY,
    results    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        # Two processes share this file: the site writes signups and cached
        # places, the melder writes delivery state. WAL lets them do that
        # without blocking each other on every read.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, user_id: str, rule: str) -> RuleState:
        with closing(
            self.connection.execute(
                "SELECT * FROM rule_state WHERE user_id = ? AND rule = ?", (user_id, rule)
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return RuleState()
        return RuleState(
            last_run_at=_parse(row["last_run_at"]),
            last_notified_at=_parse(row["last_notified_at"]),
            last_event_start=_parse(row["last_event_start"]),
        )

    def put(self, user_id: str, rule: str, state: RuleState) -> None:
        self.connection.execute(
            """
            INSERT INTO rule_state (user_id, rule, last_run_at, last_notified_at, last_event_start)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, rule) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                last_notified_at = excluded.last_notified_at,
                last_event_start = excluded.last_event_start
            """,
            (
                user_id,
                rule,
                _format(state.last_run_at),
                _format(state.last_notified_at),
                _format(state.last_event_start),
            ),
        )

    def log_delivery(
        self,
        user_id: str,
        rule: str,
        channel: str,
        title: str,
        body: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO deliveries (sent_at, user_id, rule, channel, title, body, ok, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_format(datetime.now(UTC)), user_id, rule, channel, title, body, int(ok), error),
        )

    def recent_deliveries(self, limit: int = 20) -> list[sqlite3.Row]:
        with closing(
            self.connection.execute(
                "SELECT * FROM deliveries ORDER BY sent_at DESC LIMIT ?", (limit,)
            )
        ) as cursor:
            return cursor.fetchall()

    # -- place lookups ---------------------------------------------------
    #
    # Towns do not move, so the geocoder only ever has to answer a given query
    # once. Everyone typing "Konstanz" after the first person is served from
    # here, which keeps the autocomplete quick and keeps us well inside
    # Open-Meteo's limits.

    def cached_places(self, query: str, max_age: timedelta | None = None) -> list[dict] | None:
        """Remembered results, or None if this query has not been asked before."""
        with closing(
            self.connection.execute(
                "SELECT results, fetched_at FROM places WHERE query = ?", (_key(query),)
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        if max_age is not None:
            age = datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])
            if age > max_age:
                return None
        return json.loads(row["results"])

    def remember_places(self, query: str, results: list[dict]) -> None:
        """Only worth storing a real answer: an empty one usually means the
        geocoder was unreachable, and caching that would outlast the outage."""
        if not results:
            return
        self.connection.execute(
            "INSERT INTO places (query, results, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT (query) DO UPDATE SET"
            " results = excluded.results, fetched_at = excluded.fetched_at",
            (_key(query), json.dumps(results), _format(datetime.now(UTC))),
        )

    def forget(self, user_id: str) -> None:
        self.connection.execute("DELETE FROM rule_state WHERE user_id = ?", (user_id,))


def _key(query: str) -> str:
    """Normalise so "Konstanz", " konstanz " and "KONSTANZ" share one entry."""
    folded = unicodedata.normalize("NFKC", query).casefold().strip()
    return re.sub(r"\s+", " ", folded)


def _format(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text).astimezone(UTC) if text else None
