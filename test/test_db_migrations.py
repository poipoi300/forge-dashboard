"""Schema migrations apply one transaction at a time, schema and version together."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from forge_dashboard.core import db
from forge_dashboard.core.db import Database

FIRST = "CREATE TABLE one (x INTEGER);"
BROKEN = "CREATE TABLE two (x INTEGER); CREATE TABLE two (y INTEGER);"
FIXED = "CREATE TABLE two (y INTEGER);"


def schema_of(path: Path) -> tuple[int, set[str]]:
    """The stored schema version and table names, read without going through `Database`."""
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return version, tables


async def test_a_failing_migration_leaves_neither_half_its_schema_nor_a_bumped_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    monkeypatch.setattr(db, "MIGRATIONS", (FIRST, BROKEN))

    with pytest.raises(sqlite3.OperationalError, match="table two already exists"):
        await Database.open(path)

    assert schema_of(path) == (1, {"one"})


async def test_a_migration_that_failed_is_applied_whole_once_it_is_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    monkeypatch.setattr(db, "MIGRATIONS", (FIRST, BROKEN))
    with pytest.raises(sqlite3.OperationalError):
        await Database.open(path)

    monkeypatch.setattr(db, "MIGRATIONS", (FIRST, FIXED))
    database = await Database.open(path)
    await database.close()

    assert schema_of(path) == (2, {"one", "two"})


async def test_migrations_already_applied_are_not_run_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.db"
    monkeypatch.setattr(db, "MIGRATIONS", (FIRST,))
    await (await Database.open(path)).close()

    monkeypatch.setattr(db, "MIGRATIONS", (FIRST, FIXED))
    database = await Database.open(path)
    await database.close()

    assert schema_of(path) == (2, {"one", "two"})
