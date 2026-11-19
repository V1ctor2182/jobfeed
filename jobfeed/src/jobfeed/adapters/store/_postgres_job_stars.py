"""PostgreSQL storage for independent star annotations."""

from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]

from jobfeed.domain.job_stars import JobStar


async def _read_stars(db: asyncpg.Connection, ids: list[str]) -> list[JobStar]:
    if not ids:
        return []
    group = (
        "j.id=requested.id OR (requested.real_job_id IS NOT NULL "
        "AND j.real_job_id=requested.real_job_id)"
    )
    rows = await db.fetch(
        "SELECT requested.id,requested.real_job_id,COALESCE(("
        "SELECT s.starred FROM jobs j JOIN job_stars s ON s.job_id=j.id "
        f"WHERE ({group}) ORDER BY s.user_set DESC,s.revision DESC LIMIT 1),"
        "FALSE) AS starred FROM jobs requested "
        "WHERE requested.id=ANY($1::bigint[])",
        [int(value) for value in ids],
    )
    by_id = {
        str(row["id"]): JobStar(
            str(row["id"]),
            str(row["real_job_id"]) if row["real_job_id"] else None,
            bool(row["starred"]),
        )
        for row in rows
    }
    return [by_id[source_id] for source_id in ids if source_id in by_id]


class PostgresJobStars:
    """Job star annotations on the Postgres store."""

    def _get_pool(self) -> asyncpg.Pool:
        raise NotImplementedError

    async def get_job_stars(self, source_ids: list[str]) -> list[JobStar]:
        """Read annotations without mutating application records.

        Args:
            source_ids: Source job IDs to look up.

        Returns:
            Stars for the source jobs that exist.
        """
        async with self._get_pool().acquire() as db:
            return await _read_stars(db, source_ids)

    async def set_job_star(
        self, source_id: str, starred: bool, *, user_set: bool = True
    ) -> JobStar:
        """Serialize annotation revisions without touching scan or apply locks.

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
        async with self._get_pool().acquire() as db, db.transaction():
            await db.execute("LOCK TABLE job_stars IN SHARE ROW EXCLUSIVE MODE")
            if not await _read_stars(db, [source_id]):
                raise KeyError(source_id)
            conflict = "DO NOTHING"
            if user_set:
                conflict = (
                    "DO UPDATE SET starred=excluded.starred,user_set=TRUE,"
                    "revision=excluded.revision"
                )
            await db.execute(
                "INSERT INTO job_stars(job_id,starred,user_set,revision) "
                "VALUES($1,$2,$3,(SELECT COALESCE(MAX(revision),0)+1 FROM job_stars)) "
                f"ON CONFLICT(job_id) {conflict}",
                int(source_id),
                starred,
                user_set,
            )
            return (await _read_stars(db, [source_id]))[0]
