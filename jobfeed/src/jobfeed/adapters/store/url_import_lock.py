"""Coordinate imports of one posting identity across local processes."""

from __future__ import annotations

import asyncio
import fcntl
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

from jobfeed.domain.posting_url import PostingURLKey

_RETRY_SECONDS = 0.05


def _register_slot(directory: Path, key: str) -> int:
    registry = directory / "slots.sqlite"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(registry, flags, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(registry, timeout=0)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS url_slots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, url_key TEXT NOT NULL UNIQUE)"
        )
        connection.execute(
            "INSERT INTO url_slots(url_key) VALUES(?) ON CONFLICT(url_key) DO NOTHING",
            (key,),
        )
        row = connection.execute(
            "SELECT id FROM url_slots WHERE url_key=?", (key,)
        ).fetchone()
        if row is None:
            raise RuntimeError("URL coordination registry returned no slot")
        return int(row[0])


@asynccontextmanager
async def hold_url_import(db_path: Path, key: PostingURLKey) -> AsyncIterator[None]:
    """Hold the database's posting lock until context exit or cancellation.

    Args:
        db_path: Database whose adjacent directory owns the lock registry.
        key: Posting identity shared by equivalent import requests.

    Returns:
        An async context manager that yields after acquiring the lock.

    Raises:
        sqlite3.OperationalError: Registry access fails outside lock contention.
        RuntimeError: The registry does not return the registered slot.
        OSError: Coordination files or their locks cannot be accessed.
        asyncio.CancelledError: The caller cancels while waiting or holding.
    """
    database = db_path.resolve()
    directory = database.with_name(f".{database.name}.url-import")
    directory.mkdir(mode=0o700, exist_ok=True)
    while True:
        try:
            slot = _register_slot(directory, key.concurrency_key)
            break
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) not in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise
            await asyncio.sleep(_RETRY_SECONDS)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / f"{slot}.lock", flags, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(_RETRY_SECONDS)
        yield
    finally:
        os.close(descriptor)
