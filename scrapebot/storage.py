"""Persistent state: SQLite job queue + fetch log, and JSONL item output.

SQLite makes runs resumable: a crash or Ctrl-C leaves the queue intact, URLs
are de-duplicated across runs, and the fetch log lets hourly/daily scrape
limits survive restarts.

All methods are synchronous and fast; they are only ever called from the
event-loop thread, so no locking is required.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    url_key TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    page_no INTEGER NOT NULL DEFAULT 1,
    parent_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    job_id INTEGER,
    url TEXT NOT NULL,
    identity TEXT,
    proxy TEXT,
    exit_ip TEXT,
    http_status INTEGER,
    outcome TEXT NOT NULL,
    reason TEXT,
    elapsed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS fetches_ts ON fetches(ts);
"""


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    if parts.username:
        host = f"{parts.username}@{host}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


@dataclass
class Job:
    id: int
    url: str
    target: str
    depth: int
    page_no: int
    parent_url: str | None
    attempts: int


class Store:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # --------------------------------------------------------------- jobs
    def add_job(self, url: str, target: str, *, depth: int = 0, page_no: int = 1, parent_url: str | None = None) -> bool:
        now = self.clock()
        key = f"{target}|{normalize_url(url)}"
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO jobs (url, url_key, target, depth, page_no, parent_url, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (url, key, target, depth, page_no, parent_url, now, now),
        )
        return cursor.rowcount > 0

    def add_jobs(self, urls: Iterable[str], target: str, **kwargs: Any) -> int:
        added = 0
        self.db.execute("BEGIN")
        try:
            for url in urls:
                added += self.add_job(url, target, **kwargs)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return added

    def claim(self) -> Job | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT 1",
            (self.clock(),),
        ).fetchone()
        if row is None:
            return None
        self.db.execute(
            "UPDATE jobs SET status = 'in_progress', updated_at = ? WHERE id = ?", (self.clock(), row["id"])
        )
        return Job(
            id=row["id"],
            url=row["url"],
            target=row["target"],
            depth=row["depth"],
            page_no=row["page_no"],
            parent_url=row["parent_url"],
            attempts=row["attempts"],
        )

    def _set(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = self.clock()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))

    def complete(self, job: Job) -> None:
        self._set(job.id, status="done", last_error=None)

    def fail(self, job: Job, error: str) -> None:
        self._set(job.id, status="failed", last_error=error)

    def retry(self, job: Job, delay_s: float, error: str, max_retries: int) -> bool:
        """Schedule another attempt. Returns False if the job ran out of retries."""
        attempts = job.attempts + 1
        if attempts > max_retries:
            self._set(job.id, status="failed", attempts=attempts, last_error=error)
            return False
        self._set(job.id, status="pending", attempts=attempts, last_error=error, next_attempt_at=self.clock() + delay_s)
        return True

    def release(self, job: Job, delay_s: float = 0.0) -> None:
        """Put a claimed job back without counting an attempt (e.g. on shutdown)."""
        self._set(job.id, status="pending", next_attempt_at=self.clock() + delay_s)

    def reset_in_progress(self) -> int:
        return self.db.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'in_progress'", (self.clock(),)
        ).rowcount

    def requeue_failed(self) -> int:
        return self.db.execute(
            "UPDATE jobs SET status = 'pending', attempts = 0, next_attempt_at = 0, updated_at = ? WHERE status = 'failed'",
            (self.clock(),),
        ).rowcount

    def counts(self) -> dict[str, int]:
        counts = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
        for row in self.db.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    def is_drained(self) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending', 'in_progress')"
        ).fetchone()
        return row["n"] == 0

    # ------------------------------------------------------------ fetches
    def log_fetch(
        self,
        *,
        url: str,
        outcome: str,
        job_id: int | None = None,
        identity: str | None = None,
        proxy: str | None = None,
        exit_ip: str | None = None,
        http_status: int | None = None,
        reason: str | None = None,
        elapsed_ms: int | None = None,
        ts: float | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO fetches (ts, job_id, url, identity, proxy, exit_ip, http_status, outcome, reason, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts or self.clock(), job_id, url, identity, proxy, exit_ip, http_status, outcome, reason, elapsed_ms),
        )

    def fetch_times(self, since: float) -> list[float]:
        return [r["ts"] for r in self.db.execute("SELECT ts FROM fetches WHERE ts > ? ORDER BY ts", (since,))]

    def fetch_times_by_identity(self, since: float) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        for row in self.db.execute(
            "SELECT identity, ts FROM fetches WHERE ts > ? AND identity IS NOT NULL ORDER BY ts", (since,)
        ):
            result.setdefault(row["identity"], []).append(row["ts"])
        return result

    def close(self) -> None:
        self.db.close()


class JsonlSink:
    """Appends extracted items to ``<dir>/<target>.jsonl``."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, Any] = {}
        self.items_written = 0

    def write(self, target: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        handle = self._handles.get(target)
        if handle is None:
            handle = (self.directory / f"{target}.jsonl").open("a", encoding="utf-8")
            self._handles[target] = handle
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        self.items_written += len(items)

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()
