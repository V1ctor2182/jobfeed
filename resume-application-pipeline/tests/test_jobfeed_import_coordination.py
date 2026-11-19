import asyncio
import json
import os
import signal
import sqlite3
import sys
from contextlib import AsyncExitStack

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from jobfeed.adapters.store.url_import_lock import hold_url_import
from jobfeed.domain.posting_url import PostingURLKey
from test_jobfeed_intake import ROOT, URL, rows
from test_jobfeed_intake import db as db
from test_jobfeed_intake import fixture_python as fixture_python


@pytest.fixture
def coordinated_python(fixture_python):
    root = fixture_python.parent
    launcher = root / "coordinated-python"
    source = fixture_python.read_text()
    source = source.replace(
        "import sys, runpy, httpx", "import sys, runpy, httpx, time, os, json"
    )
    source = source.replace(
        "    job_id=int",
        f"""    events = Path({str(root / "events.jsonl")!r})
    def event(phase):
        with events.open('a') as stream:
            print(json.dumps({{'phase':phase,'pid':os.getpid(),'url':str(request.url),'at':time.monotonic()}}),file=stream)
    event('start')
    time.sleep(0.4)
    event('finish')
    fail_once = Path({str(root / "fail-once")!r})
    if fail_once.exists():
        fail_once.unlink()
        return httpx.Response(503)
    job_id=int""",
    )
    launcher.write_text(source)
    launcher.chmod(0o755)
    return launcher


async def servers(stack, db, executable, count=2):
    sessions = []
    for _ in range(count):
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "pipeline.jobfeed_intake_mcp",
                "--db",
                str(db),
                "--jobfeed-root",
                str(ROOT / "jobfeed"),
                "--jobfeed-python",
                str(executable),
            ],
            env={**os.environ, "PYTHONPATH": str(ROOT / "resume-application-pipeline")},
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        sessions.append(session)
    return sessions


def events(executable):
    path = executable.parent / "events.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


async def wait_for_start(executable, count=1):
    async with asyncio.timeout(10):
        while (
            len([event for event in events(executable) if event["phase"] == "start"])
            < count
        ):
            await asyncio.sleep(0.01)


def value(result):
    assert not result.is_error
    return json.loads(result.content[0].text)


def test_independent_mcp_servers_same_database_fetch_once(db, coordinated_python):
    async def run():
        async with AsyncExitStack() as stack:
            first, second = await servers(stack, db, coordinated_python)
            a, b = await asyncio.gather(
                first.call_tool(
                    "add_job_urls", {"urls": [URL, URL + "?utm_source=first"]}
                ),
                second.call_tool(
                    "add_job_urls",
                    {"urls": [URL.replace("boards.", "job-boards."), URL]},
                ),
            )
            repeated = value(await first.call_tool("add_job_url", {"url": URL}))
            return value(a), value(b), repeated

    a, b, repeated = asyncio.run(run())
    assert a["newly_queued"] + b["newly_queued"] == 1
    assert repeated["status"] == "already_queued"
    assert (
        len(
            [event for event in events(coordinated_python) if event["phase"] == "start"]
        )
        == 1
    )
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]
    registry = db.with_name(f".{db.name}.url-import") / "slots.sqlite"
    assert rows(registry, "SELECT url_key FROM url_slots") == [
        ("native:greenhouse:123",)
    ]


def test_distinct_keys_overlap_and_leave_main_database_writable(db, coordinated_python):
    async def run():
        async with AsyncExitStack() as stack:
            first, second = await servers(stack, db, coordinated_python)
            tasks = [
                asyncio.create_task(session.call_tool("add_job_url", {"url": url}))
                for session, url in [
                    (first, URL),
                    (second, URL.replace("/123", "/124")),
                ]
            ]
            await wait_for_start(coordinated_python, 2)
            with sqlite3.connect(db, timeout=0) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return [value(item) for item in await asyncio.gather(*tasks)]

    results = asyncio.run(run())
    assert [result["status"] for result in results] == ["queued", "queued"]
    observed = events(coordinated_python)
    starts = [event["at"] for event in observed if event["phase"] == "start"]
    finishes = [event["at"] for event in observed if event["phase"] == "finish"]
    assert max(starts) < min(finishes)
    assert rows(db, "SELECT count(*) FROM application_queue") == [(2,)]


def test_killed_import_owner_releases_waiter(db, coordinated_python):
    async def run():
        async with AsyncExitStack() as stack:
            first, second = await servers(stack, db, coordinated_python)
            owner = asyncio.create_task(first.call_tool("add_job_url", {"url": URL}))
            await wait_for_start(coordinated_python)
            waiter = asyncio.create_task(second.call_tool("add_job_url", {"url": URL}))
            os.kill(events(coordinated_python)[0]["pid"], signal.SIGKILL)
            return value(await owner), value(await asyncio.wait_for(waiter, 10))

    owner, waiter = asyncio.run(run())
    assert owner["status"] == "error"
    assert waiter["status"] == "queued"
    assert (
        len(
            [event for event in events(coordinated_python) if event["phase"] == "start"]
        )
        == 2
    )
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]


def test_failed_owner_allows_fresh_retry(db, coordinated_python):
    (coordinated_python.parent / "fail-once").touch()

    async def run():
        async with AsyncExitStack() as stack:
            first, second = await servers(stack, db, coordinated_python)
            return [
                value(result)
                for result in await asyncio.gather(
                    first.call_tool("add_job_url", {"url": URL}),
                    second.call_tool(
                        "add_job_url", {"url": URL + "?utm_source=second"}
                    ),
                )
            ]

    results = asyncio.run(run())
    assert sorted(result["status"] for result in results) == ["needs_details", "queued"]
    assert (
        len(
            [event for event in events(coordinated_python) if event["phase"] == "start"]
        )
        == 2
    )
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]


def test_cancelled_holder_and_waiter_release_descriptors(db):
    key = PostingURLKey.parse(URL)

    async def run():
        held = asyncio.Event()

        async def owner():
            async with hold_url_import(db, key):
                held.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(owner())
        await held.wait()

        async def waiter():
            async with hold_url_import(db, key):
                return "acquired"

        waiting = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(waiter(), 2) == "acquired"

    asyncio.run(run())


def test_wait_timeout_does_not_bypass_holder(db):
    key = PostingURLKey.parse(URL)

    async def run():
        async with hold_url_import(db, key):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.1), hold_url_import(db, key):
                    pytest.fail("Waiter bypassed a held URL lock")
        async with asyncio.timeout(2), hold_url_import(db, key):
            pass

    asyncio.run(run())


def test_database_symlink_uses_same_coordination_namespace(db, tmp_path):
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(db)
    key = PostingURLKey.parse(URL)

    async def run():
        async with hold_url_import(db, key):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.1), hold_url_import(alias, key):
                    pytest.fail("Symlink created a second lock namespace")

    asyncio.run(run())
    assert not alias.with_name(f".{alias.name}.url-import").exists()


def test_mcp_cancellation_releases_import_owner(db, coordinated_python):
    async def run():
        async with AsyncExitStack() as stack:
            first, second = await servers(stack, db, coordinated_python)
            owner = asyncio.create_task(first.call_tool("add_job_url", {"url": URL}))
            await wait_for_start(coordinated_python)
            pid = events(coordinated_python)[0]["pid"]
            waiter = asyncio.create_task(second.call_tool("add_job_url", {"url": URL}))
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            result = value(await asyncio.wait_for(waiter, 10))
            return pid, result

    pid, result = asyncio.run(run())
    assert result["status"] == "queued"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert (
        len(
            [event for event in events(coordinated_python) if event["phase"] == "start"]
        )
        == 2
    )
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]


def test_long_full_keys_use_integer_files_without_url_filenames(db):
    key = PostingURLKey.parse(
        "https://careers.example.com/posting?identity=" + "a" * 5000
    )

    async def run():
        async with hold_url_import(db, key):
            directory = db.with_name(f".{db.name}.url-import")
            assert (directory / "1.lock").is_file()
            assert rows(
                directory / "slots.sqlite", "SELECT url_key FROM url_slots"
            ) == [(key.concurrency_key,)]

    asyncio.run(run())
