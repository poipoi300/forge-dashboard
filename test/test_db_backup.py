"""Snapshots of the live database: taken between writes, never deadlocked by them."""

import asyncio
from pathlib import Path

import pytest

from forge_dashboard.core.db import Database


async def test_a_backup_taken_while_another_task_writes_finishes_and_holds_the_data(tmp_path: Path) -> None:
    database = await Database.open(tmp_path / "live.sqlite3")
    stop = asyncio.Event()

    async def writer() -> None:
        count = 0
        while not stop.is_set():
            await database.kv_put("probe", str(count % 20), {"count": count})
            count += 1

    task = asyncio.create_task(writer())
    try:
        await asyncio.sleep(0.05)
        for index in range(5):
            # The page-copy API hung here whenever the snapshot landed between a write and its commit.
            await asyncio.wait_for(database.backup_to(tmp_path / f"backup-{index}.sqlite3"), timeout=10)
    finally:
        stop.set()
        await task
    await database.close()

    snapshot = await Database.open(tmp_path / "backup-4.sqlite3")
    assert len(await snapshot.kv_all("probe")) == 20
    await snapshot.close()


async def test_a_backup_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    database = await Database.open(tmp_path / "live.sqlite3")
    existing = tmp_path / "taken.sqlite3"
    existing.write_bytes(b"keep me")

    with pytest.raises(FileExistsError):
        await database.backup_to(existing)
    await database.close()

    assert existing.read_bytes() == b"keep me"
