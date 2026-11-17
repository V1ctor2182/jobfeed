"""Stars persist independently of scan refreshes and application queue state."""

from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from jobfeed.adapters.store._sqlite_real_job_identity import _merge
from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.import_url import star_imported
from tests.support.factories import make_job


async def test_scanner_defaults_false_and_refresh_preserves_user_choice(tmp_path: Path):
    store = SQLiteStore(tmp_path / "stars.db")
    await store.connect()
    try:
        posting = make_job()
        saved = await store.save_job(posting)
        source = saved.job_id
        assert not (await store.get_job_stars([source]))[0].starred
        assert (await store.set_job_star(source, True)).starred
        await store.save_job(replace(posting, jd_text="Updated scanner description"))
        assert (await store.get_job_stars([source]))[0].starred
        assert not (await store.set_job_star(source, False)).starred
        await store.save_job(posting)
        assert not (await store.get_job_stars([source]))[0].starred
        async with aiosqlite.connect(tmp_path / "stars.db") as db:
            assert (
                await (
                    await db.execute(
                        "SELECT name FROM sqlite_master WHERE name='application_queue'"
                    )
                ).fetchone()
                is None
            )
    finally:
        await store.close()


async def test_scanner_queue_promotion_does_not_star_and_toggle_leaves_queue_unchanged(
    tmp_path: Path,
):
    path = tmp_path / "stars.db"
    store = SQLiteStore(path)
    await store.connect()
    try:
        source = (await store.save_job(make_job())).job_id
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY,"
                "state TEXT,manual_priority INTEGER,submit_authorization TEXT)"
            )
            await db.execute(
                "INSERT INTO application_queue VALUES (?,'Queued',1,NULL)", (source,)
            )
            await db.commit()
        assert not (await store.get_job_stars([source]))[0].starred
        assert not (await store.set_job_star(source, False)).starred
        await store.set_job_star(source, True)
        async with aiosqlite.connect(path) as db:
            assert await (
                await db.execute("SELECT * FROM application_queue")
            ).fetchall() == [(int(source), "Queued", 1, None)]
    finally:
        await store.close()


async def test_merge_and_new_siblings_follow_latest_explicit_choice(tmp_path: Path):
    store = SQLiteStore(tmp_path / "stars.db")
    await store.connect()
    try:
        first = (
            await store.save_job(
                make_job(canonical_id="first", url="https://one.test/job")
            )
        ).job_id
        second = (
            await store.save_job(
                make_job(canonical_id="second", url="https://two.test/job")
            )
        ).job_id
        left = await store.resolve_real_job_id(first)
        right = await store.resolve_real_job_id(second)
        assert left is not None and right is not None and left != right
        await store.set_job_star(first, True)
        await store.set_job_star(second, False)
        async with store._lifecycle.connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await _merge(db, int(left), int(right))
            await db.commit()
        assert [row.starred for row in await store.get_job_stars([first, second])] == [
            False,
            False,
        ]
        await store.set_job_star(first, True)
        assert [row.starred for row in await store.get_job_stars([first, second])] == [
            True,
            True,
        ]
        await store.set_job_star(second, False)
        await store.set_job_star(first, True, user_set=False)
        assert [row.starred for row in await store.get_job_stars([first, second])] == [
            False,
            False,
        ]
    finally:
        await store.close()


async def test_manual_import_defaults_star_without_queue_and_preserves_unstar(
    tmp_path: Path,
):
    path = tmp_path / "stars.db"
    store = SQLiteStore(path)
    await store.connect()
    try:
        source = (await store.save_job(make_job())).job_id
        assert await star_imported(path, int(source)) == {
            "status": "starred",
            "source_job_id": int(source),
            "starred": True,
        }
        await store.set_job_star(source, False)
        assert (await star_imported(path, int(source)))["starred"] is False
        assert await store.get_job_stars(["999999"]) == []
        with pytest.raises(KeyError):
            await store.set_job_star("999999", True)
    finally:
        await store.close()


async def test_existing_schema_adds_annotation_table_without_losing_jobs(
    tmp_path: Path,
):
    path = tmp_path / "stars.db"
    store = SQLiteStore(path)
    await store.connect()
    source = (await store.save_job(make_job())).job_id
    await store.close()
    async with aiosqlite.connect(path) as db:
        await db.execute("DROP TABLE job_stars")
        await db.commit()
    await store.connect()
    try:
        assert not (await store.get_job_stars([source]))[0].starred
        assert (await store.set_job_star(source, True)).starred
    finally:
        await store.close()


async def test_queue_promotion_and_manual_import_keep_independent_star_defaults(
    tmp_path: Path,
):
    path = tmp_path / "stars.db"
    store = SQLiteStore(path)
    await store.connect()
    try:
        scanner_posting = make_job(canonical_id="scanner", url="https://scan.test/job")
        manual_posting = make_job(canonical_id="manual", url="https://manual.test/job")
        scanner = (await store.save_job(scanner_posting)).job_id
        manual = (await store.save_job(manual_posting)).job_id
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY,"
                "state TEXT,manual_priority INTEGER)"
            )
            await db.executemany(
                "INSERT INTO application_queue VALUES (?,'Queued',1)",
                [(scanner,), (manual,)],
            )
            await db.commit()
        stars = await store.get_job_stars([scanner, manual])
        assert [row.starred for row in stars] == [
            False,
            False,
        ]
        await star_imported(path, int(manual))
        stars = await store.get_job_stars([scanner, manual])
        assert [row.starred for row in stars] == [
            False,
            True,
        ]
        await store.set_job_star(scanner, True)
        await store.set_job_star(manual, False)
        await store.save_job(replace(scanner_posting, jd_text="Refreshed scanner role"))
        await store.save_job(replace(manual_posting, jd_text="Refreshed imported role"))
        await star_imported(path, int(manual))
        stars = await store.get_job_stars([scanner, manual])
        assert [row.starred for row in stars] == [
            True,
            False,
        ]
        async with aiosqlite.connect(path) as db:
            assert await (
                await db.execute("SELECT * FROM application_queue ORDER BY job_id")
            ).fetchall() == [
                (int(scanner), "Queued", 1),
                (int(manual), "Queued", 1),
            ]
    finally:
        await store.close()
