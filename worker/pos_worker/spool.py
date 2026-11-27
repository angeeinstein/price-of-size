"""Local SQLite spool: results wait here until the coordinator acknowledges them.

A node that loses its connection keeps working (up to `max_results` spooled),
then flushes when the API is reachable again. Also remembers which one-off
commands already ran, so each runs at most once even across restarts.
"""

import json
import sqlite3
import threading
from pathlib import Path


class Spool:
    def __init__(self, path: str | Path, max_results: int):
        self.max_results = max_results
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            -- One pending result per job: a newer attempt replaces an older one,
            -- which the coordinator would reject as superseded anyway.
            CREATE TABLE IF NOT EXISTS result (
                job_id TEXT PRIMARY KEY,
                attempt INTEGER NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );
            CREATE TABLE IF NOT EXISTS command_done (
                id TEXT PRIMARY KEY,
                ok INTEGER NOT NULL,
                output TEXT,
                reported INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    # results -------------------------------------------------------------

    def add_result(self, job_id: str, attempt: int, body: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO result (job_id, attempt, body) VALUES (?, ?, ?)",
                (job_id, attempt, json.dumps(body)),
            )

    def peek_results(self, limit: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM result ORDER BY created_at LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def remove_results(self, sent: list[tuple[str, int]]) -> None:
        """Delete results by (job_id, attempt), leaving newer attempts in place."""
        if not sent:
            return
        with self._lock:
            self._db.executemany("DELETE FROM result WHERE job_id = ? AND attempt = ?", sent)

    def result_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM result").fetchone()[0]

    @property
    def full(self) -> bool:
        return self.result_count() >= self.max_results

    # commands ------------------------------------------------------------

    def command_seen(self, cmd_id: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM command_done WHERE id = ?", (cmd_id,)).fetchone()
                is not None
            )

    def record_command(self, cmd_id: str, ok: bool, output) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO command_done (id, ok, output) VALUES (?, ?, ?)",
                (cmd_id, int(ok), json.dumps(output)),
            )

    def unreported_commands(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ok, output FROM command_done WHERE reported = 0 LIMIT 100"
            ).fetchall()
        return [{"id": r[0], "ok": bool(r[1]), "output": json.loads(r[2])} for r in rows]

    def mark_commands_reported(self, ids: list[str]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE command_done SET reported = 1 WHERE id = ?", [(i,) for i in ids]
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
