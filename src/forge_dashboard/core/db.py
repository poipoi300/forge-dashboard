"""SQLite state store (WAL), accessed asynchronously.

All dashboard state lives here: service configs, per-project user state, the
event log, jobs, schedules, check and metric history, fork and agent runs,
and the GitHub response cache. Schema changes are numbered migrations applied
in order and tracked in `PRAGMA user_version`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import JsonValue

SqlParams = Sequence[Any] | dict[str, Any]

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE kv (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        service TEXT NOT NULL,
        type TEXT NOT NULL,
        project_id TEXT,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        data TEXT NOT NULL,
        notify INTEGER NOT NULL DEFAULT 0,
        read INTEGER NOT NULL DEFAULT 0,
        dedupe_key TEXT UNIQUE
    );
    CREATE INDEX events_project_ts ON events (project_id, ts);
    CREATE INDEX events_ts ON events (ts);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        project_id TEXT,
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        started_at REAL,
        ended_at REAL,
        summary TEXT,
        error TEXT,
        result TEXT,
        log_path TEXT NOT NULL
    );
    CREATE INDEX jobs_project_created ON jobs (project_id, created_at);
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        entry TEXT NOT NULL
    );
    CREATE TABLE check_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        check_id TEXT NOT NULL,
        job_id TEXT,
        started_at REAL NOT NULL,
        duration_s REAL NOT NULL,
        outcome TEXT NOT NULL,
        exit_code INTEGER,
        sha TEXT,
        dirty INTEGER NOT NULL,
        on_head INTEGER NOT NULL,
        tests INTEGER,
        failures INTEGER,
        errors INTEGER,
        skipped INTEGER,
        details TEXT NOT NULL
    );
    CREATE INDEX check_runs_project_check ON check_runs (project_id, check_id, started_at);
    CREATE TABLE metric_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        metric_id TEXT NOT NULL,
        ts REAL NOT NULL,
        value TEXT,
        status TEXT,
        detail TEXT,
        error TEXT
    );
    CREATE INDEX metric_readings_project_metric ON metric_readings (project_id, metric_id, ts);
    CREATE TABLE github_cache (
        url TEXT PRIMARY KEY,
        etag TEXT,
        status INTEGER NOT NULL,
        body TEXT NOT NULL,
        fetched_at REAL NOT NULL
    );
    CREATE TABLE agent_runs (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        project_id TEXT,
        started_at REAL NOT NULL,
        ended_at REAL,
        model TEXT,
        outcome TEXT,
        exit_code INTEGER,
        cost_usd REAL,
        turns INTEGER,
        duration_ms INTEGER,
        transcript_path TEXT NOT NULL
    );
    CREATE INDEX agent_runs_project ON agent_runs (project_id, started_at);
    CREATE TABLE fork_runs (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        state TEXT NOT NULL,
        record TEXT NOT NULL
    );
    CREATE INDEX fork_runs_project ON fork_runs (project_id, started_at);
    CREATE TABLE branch_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        job_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        state TEXT NOT NULL,
        record TEXT NOT NULL
    );
    CREATE INDEX branch_runs_project ON branch_runs (project_id, kind, started_at);
    """,
)


class Database:
    """One aiosqlite connection in WAL mode, with JSON key-value helpers."""

    def __init__(self, connection: aiosqlite.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path
        # A write is its statement plus its commit. Tasks share one connection, so without this lock
        # another task's statement could land between them, and a snapshot taken there would find a
        # transaction open that can only close after the snapshot finishes: a deadlock.
        self._writing = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = sqlite3.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA busy_timeout=5000")
        database = cls(connection, path)
        try:
            await database._migrate()
        except BaseException:
            await connection.close()
            raise
        return database

    async def _migrate(self) -> None:
        async with self._connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = int(row[0]) if row is not None else 0
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # One transaction per migration, so a failure leaves neither half its schema nor a bumped version.
            try:
                await self._connection.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index};\nCOMMIT;")
            except BaseException:
                await self._connection.rollback()
                raise

    async def close(self) -> None:
        await self._connection.close()

    async def execute(self, sql: str, params: SqlParams = ()) -> int:
        """Run one statement, commit, and return lastrowid."""
        async with self._writing:
            cursor = await self._connection.execute(sql, params)
            await self._connection.commit()
            return cursor.lastrowid or 0

    async def execute_many(self, sql: str, rows: Iterable[SqlParams]) -> None:
        async with self._writing:
            await self._connection.executemany(sql, rows)
            await self._connection.commit()

    async def fetch_all(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        async with self._connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        async with self._connection.execute(sql, params) as cursor:
            row: sqlite3.Row | None = await cursor.fetchone()
            return row

    # --- key-value documents ---

    async def kv_get(self, namespace: str, key: str) -> JsonValue | None:
        row = await self.fetch_one("SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
        return None if row is None else json.loads(row["value"])

    async def kv_put(self, namespace: str, key: str, value: JsonValue) -> None:
        await self.execute(
            "INSERT INTO kv (namespace, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (namespace, key, json.dumps(value), time.time()),
        )

    async def kv_delete(self, namespace: str, key: str) -> None:
        await self.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))

    async def kv_all(self, namespace: str) -> dict[str, JsonValue]:
        rows = await self.fetch_all("SELECT key, value FROM kv WHERE namespace = ?", (namespace,))
        return {row["key"]: json.loads(row["value"]) for row in rows}

    # --- backup ---

    async def backup_to(self, destination: Path) -> None:
        """Write a consistent online snapshot of the database to `destination`, which must not exist yet.

        `VACUUM INTO` runs on the dashboard's own connection between writes, so
        it never meets a transaction still waiting for its commit.
        """
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._writing:
            await self._connection.execute("VACUUM INTO ?", (str(destination),))

    async def restore_from(self, source: Path) -> None:
        """Replace the live database's content with a snapshot, in place, between writes."""
        async with self._writing:
            snapshot = await aiosqlite.connect(source)
            try:
                await snapshot.backup(self._connection)
            finally:
                await snapshot.close()
            await self._migrate()
