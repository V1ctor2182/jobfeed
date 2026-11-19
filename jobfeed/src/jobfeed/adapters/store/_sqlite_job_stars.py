"""Source annotations projected across current canonical siblings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from jobfeed.adapters.store._sqlite_capability_support import _immediate_transaction
from jobfeed.domain.job_stars import JobStar

if TYPE_CHECKING:
    from jobfeed.adapters.store.sqlite_lifecycle import SqliteLifecycle


async def _read_stars(db: aiosqlite.Connection, ids: list[str]) -> list[JobStar]:
    if not ids:
        return []
    cursor = await db.execute("PRAGMA table_info(jobs)")
    canonical = "real_job_id" in {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    group = "j.id=requested.id"
    if canonical:
        group += (
            " OR (requested.real_job_id IS NOT NULL "
            "AND j.real_job_id=requested.real_job_id)"
        )
    parent = "requested.real_job_id" if canonical else "NULL"
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"SELECT requested.id,{parent},COALESCE(("
        "SELECT s.starred FROM jobs j JOIN job_stars s ON s.job_id=j.id "
        f"WHERE ({group}) ORDER BY s.user_set DESC,s.revision DESC LIMIT 1),"
        f"0) FROM jobs requested WHERE requested.id IN ({placeholders})",
        ids,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    by_id = {
        str(row[0]): JobStar(str(row[0]), str(row[1]) if row[1] else None, bool(row[2]))
        for row in rows
    }
    return [by_id[source_id] for source_id in ids if source_id in by_id]


class SqliteJobStars:
    """Job star annotations on the SQLite store."""

    _lifecycle: SqliteLifecycle

    async def get_job_stars(self, source_ids: list[str]) -> list[JobStar]:
        """Read annotations without creating queue records or changing workflow.

        Args:
            source_ids: Source job IDs to look up.

        Returns:
            Stars for the source jobs that exist.
        """
        async with self._lifecycle.connection() as db:
            return await _read_stars(db, source_ids)

    async def set_job_star(
        self, source_id: str, starred: bool, *, user_set: bool = True
    ) -> JobStar:
        """Persist a user override or an import default in one short transaction.

        Args:
            source_id: Source job ID to annotate.
            starred: Whether the job should be starred.
            user_set: True for a user choice, which overrides an existing star; False
                for an import default, which never replaces one.

        Returns:
            The job's star after the write.

        Raises:
            KeyError: If the source job does not exist.
        """
        async with self._lifecycle.connection() as db, _immediate_transaction(db):
            if not await _read_stars(db, [source_id]):
                raise KeyError(source_id)
            conflict = "DO NOTHING"
            if user_set:
                conflict = (
                    "DO UPDATE SET starred=excluded.starred,user_set=1,"
                    "revision=excluded.revision"
                )
            await db.execute(
                "INSERT INTO job_stars(job_id,starred,user_set,revision) "
                "VALUES(?,?,?,(SELECT COALESCE(MAX(revision),0)+1 FROM job_stars)) "
                f"ON CONFLICT(job_id) {conflict}",
                (int(source_id), int(starred), int(user_set)),
            )
            return (await _read_stars(db, [source_id]))[0]
