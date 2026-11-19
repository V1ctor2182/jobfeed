import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.domain.models import JobPosting
from jobfeed.import_url import initialize
from jobfeed.adapters.url_import import ImportHTTPClient, JobUrlImportService
from pipeline.jobfeed_intake import JobfeedIntake
from pipeline.store import ApplicationQueue

ROOT = Path(__file__).resolve().parents[2]
URL = "https://boards.greenhouse.io/acme/jobs/123"
JD = "Build accessible software. Requirements: Python and SQL. Full time, New York."


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.sqlite"
    asyncio.run(initialize(path))
    return path


async def fixture_guard(url):
    return httpx.URL(url).host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards-api.greenhouse.io",
        "careers.example.com",
    }


def fixture_response(request):
    return httpx.Response(
        200,
        json={
            "id": 123,
            "title": "Software Engineer",
            "company_name": "Acme",
            "absolute_url": URL,
            "content": JD,
            "location": {"name": "New York"},
        },
    )


async def import_fixture(db, url=URL, handler=fixture_response):
    async with ImportHTTPClient(
        url_guard=fixture_guard, transport=httpx.MockTransport(handler)
    ) as client:
        return await JobUrlImportService(db, client=client).import_url(url)


def queue(db):
    result = ApplicationQueue(db)
    result.initialize()
    return result


def rows(db, sql, args=()):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


def write(db, sql, args=()):
    with sqlite3.connect(db) as conn:
        conn.execute(sql, args)


async def save_sibling(db):
    store = SQLiteStore(db)
    await store.connect()
    try:
        result = await store.save_job(
            JobPosting(
                platform="linkedin",
                canonical_id="987",
                url="https://linkedin.com/jobs/view/987",
                apply_url=URL,
                title="Software Engineer",
                company="Acme",
                location="New York",
                discovered_at=datetime.now(UTC),
                jd_text=JD,
            )
        )
        return int(result.job_id)
    finally:
        await store.close()


async def save_distinct_portal(db):
    sibling = await save_sibling(db)
    write(db, "UPDATE jobs SET apply_url=url,external_identity=NULL WHERE id=?", (sibling,))
    return sibling


def test_repeated_alias_import_and_reopen(db):
    first = asyncio.run(import_fixture(db))
    q = queue(db)
    assert q.admit(first.source_job_id).status == "queued"
    again = asyncio.run(
        import_fixture(
            db, "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=claude"
        )
    )
    assert again.source_job_id == first.source_job_id
    assert q.admit(again.source_job_id).status == "already_queued"
    asyncio.run(initialize(db))
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]
    assert rows(db, "SELECT title,company,jd_text FROM jobs") == [
        ("Software Engineer", "Acme", JD)
    ]


def test_existing_scanned_source_is_reused(db):
    sibling = asyncio.run(save_sibling(db))
    queue(db).enqueue(sibling)
    imported = asyncio.run(import_fixture(db))
    assert imported.source_job_id == sibling
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert queue(db).admit(imported.source_job_id).queue_job_id == sibling


@pytest.mark.parametrize(
    "state",
    [
        "Applying",
        "Submitted",
        "Cancelled",
        "Skipped",
        "Unavailable",
        "Failed",
        "NeedsInput",
    ],
)
def test_existing_states_are_preserved(db, state):
    imported = asyncio.run(import_fixture(db))
    q = queue(db)
    q.enqueue(imported.source_job_id)
    write(db, "UPDATE application_queue SET state=?", (state,))
    result = q.admit(imported.source_job_id)
    assert (result.status, result.queue_state) == ("already_queued", state)
    assert rows(db, "SELECT state FROM application_queue") == [(state,)]


@pytest.mark.parametrize(
    "state",
    [
        "identity_conflict",
        "requirements_conflict",
        "evaluation_input_missing",
        "status_conflict",
    ],
)
def test_review_flags_admission(db, state):
    imported = asyncio.run(import_fixture(db))
    write(db, "UPDATE real_jobs SET identity_review_state=?", (state,))
    result = queue(db).admit(imported.source_job_id)
    assert result.status == "queued"
    assert result.identity_warnings == ("identity_review:" + state,)
    assert queue(db).admit(imported.source_job_id).identity_warnings == result.identity_warnings
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]


def test_canonical_only_receipt_flags(db):
    imported = asyncio.run(import_fixture(db))
    write(
        db,
        "INSERT INTO real_job_applications(real_job_id,applied_at) VALUES(?,?)",
        (imported.real_job_id, "2026-10-20T12:00:00Z"),
    )
    result = queue(db).admit(imported.source_job_id)
    assert result.status == "queued"
    assert "canonical_application_recorded" in result.identity_warnings


@pytest.mark.parametrize(
    "sql,expected",
    [
        ('UPDATE jobs SET closed_at="2026-10-20T12:00:00Z"', 'closed'),
        ('UPDATE real_jobs SET official_closed_at="2026-10-20T12:00:00Z"', 'queued'),
    ],
)
def test_closed_source_blocks_but_parent_only_flags(db, sql, expected):
    imported = asyncio.run(import_fixture(db))
    write(db, sql)
    asyncio.run(import_fixture(db))
    result = queue(db).admit(imported.source_job_id)
    assert result.status == expected
    if expected == "queued":
        assert "canonical_closed" in result.identity_warnings


def test_whitespace_description_blocked(db):
    imported = asyncio.run(import_fixture(db))
    write(db, 'UPDATE jobs SET jd_text=" "')
    assert queue(db).admit(imported.source_job_id).status == "needs_details"


def test_private_redirect_is_rejected(db):
    def redirect(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})

    result = asyncio.run(
        import_fixture(db, "https://careers.example.com/123", redirect)
    )
    assert result.status == "needs_details"
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://169.254.169.254/",
        "https://user:password@example.com",
    ],
)
def test_private_initial_url_rejected(db, url):
    result = asyncio.run(JobUrlImportService(db).import_url(url))
    assert result.status == "needs_details"


def test_json_ld_requires_real_matching_single_posting(db):
    node = {
        "@type": "JobPosting",
        "title": "Engineer",
        "hiringOrganization": {"name": "Actual Company"},
        "description": JD,
        "url": "https://careers.example.com/123",
    }

    def response(request):
        return httpx.Response(
            200,
            text='<script type="application/ld+json">' + json.dumps(node) + "</script>",
        )

    imported = asyncio.run(
        import_fixture(db, "https://careers.example.com/123", response)
    )
    assert imported.status == "imported"
    assert rows(db, "SELECT company FROM jobs") == [("Actual Company",)]
    node["url"] = "https://careers.example.com/different"
    result = asyncio.run(
        import_fixture(db, "https://careers.example.com/456", response)
    )
    assert result.status == "needs_details"
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]


def test_search_page_and_oversized_body_do_not_write(db):
    result = asyncio.run(
        import_fixture(
            db,
            "https://careers.example.com/search",
            lambda r: httpx.Response(200, text="Search jobs"),
        )
    )
    assert result.status == "needs_details"
    result = asyncio.run(
        import_fixture(
            db,
            "https://careers.example.com/large",
            lambda r: httpx.Response(200, content=b"x" * 2_000_001),
        )
    )
    assert result.status == "needs_details"
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_parallel_processes_admit_distinct_portals_once_each(db):
    imported = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_distinct_portal(db))
    queue(db)
    assert rows(db, "SELECT count(DISTINCT real_job_id) FROM jobs") == [(1,)]
    script = "from pathlib import Path; from pipeline.store import ApplicationQueue; import sys; print(ApplicationQueue(Path(sys.argv[1])).admit(int(sys.argv[2])).status)"

    async def race():
        procs = [
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(db),
                str(job),
                stdout=asyncio.subprocess.PIPE,
            )
            for job in [sibling, imported.source_job_id] * 3
        ]
        return [
            ((await proc.communicate())[0].decode().strip(), proc.returncode)
            for proc in procs
        ]

    results = asyncio.run(race())
    assert all(code == 0 for _, code in results)
    assert [status for status, _ in results].count("queued") == 2
    assert [status for status, _ in results].count("already_queued") == 4
    assert rows(db, "SELECT count(*) FROM application_queue") == [(2,)]


@pytest.fixture
def fixture_python(tmp_path):
    launcher = tmp_path / "fixture-python"
    launcher.write_text(f"""#!{sys.executable}
import sys, runpy, httpx
from pathlib import Path
from jobfeed.adapters import url_import
RealClient = url_import.ImportHTTPClient
async def guard(url):
    return httpx.URL(url).host in {{'boards.greenhouse.io','job-boards.greenhouse.io','boards-api.greenhouse.io'}}
def respond(request):
    with Path({str(tmp_path / "fetches.txt")!r}).open('a') as log:
        print(str(request.url), file=log)
    job_id=int(request.url.path.rsplit('/',1)[-1])
    title='Software Engineer' if job_id == 123 else 'Data Engineer'
    return httpx.Response(200,json={{'id':job_id,'title':title,'company_name':'Acme','absolute_url':'https://boards.greenhouse.io/acme/jobs/'+str(job_id),'content':{JD!r}}})
class FixtureClient(RealClient):
    def __init__(self):
        super().__init__(url_guard=guard,transport=httpx.MockTransport(respond))
url_import.ImportHTTPClient=FixtureClient
module=sys.argv[2]
sys.argv=[module,*sys.argv[3:]]
runpy.run_module(module,run_name='__main__')
""")
    launcher.chmod(0o755)
    return launcher


def test_failure_after_save_converges(db, fixture_python, monkeypatch):
    intake = JobfeedIntake(db, ROOT / "jobfeed", fixture_python)
    original = ApplicationQueue.admit

    def failed(*args, **kwargs):
        raise sqlite3.OperationalError("fixture queue failure")

    monkeypatch.setattr(ApplicationQueue, "admit", failed)
    first = asyncio.run(intake.add_job_url(URL))
    assert first["status"] == "imported_not_queued"
    assert first["source_job_id"] == 1
    monkeypatch.setattr(ApplicationQueue, "admit", original)
    second = asyncio.run(intake.add_job_url(URL))
    assert second["status"] == "queued"
    assert second["source_job_id"] == 1
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]


def test_stdio_mcp_import_duplicate_and_no_dispatch(db, fixture_python):
    async def exercise():
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
                str(fixture_python),
            ],
            env={**os.environ, "PYTHONPATH": str(ROOT / "resume-application-pipeline")},
        )
        async with stdio_client(params) as (read, write_stream):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {"add_job_url", "add_job_urls", "prioritize_jobs", "capabilities"}
                results = []
                for url in (
                    URL,
                    "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=claude",
                ):
                    result = await session.call_tool("add_job_url", {"url": url})
                    assert not result.is_error
                    results.append(json.loads(result.content[0].text))
                return results

    first, second = asyncio.run(exercise())
    assert first["status"] == "queued"
    assert second["status"] == "already_queued"
    assert first["source_job_id"] == second["source_job_id"] == 1
    assert rows(
        db,
        "SELECT state,codex_application_session_id,submit_authorization FROM application_queue",
    ) == [("Queued", None, None)]


def test_compressed_posting_is_decoded_once(db):
    import gzip

    payload = {
        "id": 123,
        "title": "Software Engineer",
        "company_name": "Acme",
        "absolute_url": URL,
        "content": JD,
    }

    def response(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(json.dumps(payload).encode()),
        )

    result = asyncio.run(import_fixture(db, handler=response))
    assert result.status == "imported"
    assert rows(db, "SELECT jd_text FROM jobs") == [(JD,)]


@pytest.mark.parametrize(
    "workflow,expected", [("applied", "queued"), ("ignored", "needs_details")]
)
def test_workflow_status_without_receipt_is_preserved(db, workflow, expected):
    imported = asyncio.run(import_fixture(db))
    write(
        db,
        "UPDATE real_job_status SET status=? WHERE real_job_id=?",
        (workflow, imported.real_job_id),
    )
    result = queue(db).admit(imported.source_job_id)
    assert result.status == expected
    if workflow == "applied":
        assert "canonical_status_applied" in result.identity_warnings
    assert rows(db, "SELECT count(*) FROM application_queue") == [(int(expected == "queued"),)]


def test_closed_aggregator_does_not_block_open_official_source(db):
    sibling = asyncio.run(save_sibling(db))
    write(db, 'UPDATE jobs SET closed_at="2026-10-20T12:00:00Z" WHERE id=?', (sibling,))
    imported = asyncio.run(import_fixture(db))
    assert imported.status == "imported"
    assert imported.source_job_id != sibling
    assert queue(db).admit(imported.source_job_id).status == "queued"
    assert rows(db, "SELECT closed_at FROM jobs WHERE id=?", (sibling,)) == [
        ("2026-10-20T12:00:00Z",)
    ]


@pytest.mark.parametrize(
    "vendor,url,body",
    [
        (
            "ashby",
            "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
            {
                "jobs": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "title": "Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
                        "descriptionPlain": JD,
                    }
                ]
            },
        ),
        (
            "lever",
            "https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
            [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "text": "Engineer",
                    "hostedUrl": "https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
                    "descriptionPlain": JD,
                }
            ],
        ),
    ],
)
def test_native_board_import_selects_requested_id(db, vendor, url, body):
    async def run():
        async def guard(target):
            return httpx.URL(target).host in {
                "jobs.ashbyhq.com",
                "api.ashbyhq.com",
                "jobs.lever.co",
                "api.lever.co",
            }

        async with ImportHTTPClient(
            url_guard=guard,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ),
        ) as client:
            return await JobUrlImportService(db, client=client).import_url(url)

    result = asyncio.run(run())
    assert result.status == "imported"
    assert rows(db, "SELECT platform,canonical_id FROM jobs") == [
        (vendor, "11111111-1111-1111-1111-111111111111")
    ]
    assert queue(db).admit(result.source_job_id).status == "queued"


def test_concurrent_first_imports_converge(db, fixture_python):
    intake = JobfeedIntake(db, ROOT / "jobfeed", fixture_python)

    async def run():
        return await asyncio.gather(
            intake.add_job_url(URL), intake.add_job_url(URL + "?utm_source=claude")
        )

    results = asyncio.run(run())
    assert sorted(result["status"] for result in results) == [
        "already_queued",
        "queued",
    ]
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(db, "SELECT count(*) FROM real_jobs") == [(1,)]
    assert rows(db, "SELECT count(*) FROM application_queue") == [(1,)]


def test_open_matching_source_precedes_older_closed_source(db):
    official = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_sibling(db))
    write(
        db,
        'UPDATE jobs SET closed_at="2026-10-20T12:00:00Z" WHERE id=?',
        (official.source_job_id,),
    )
    assert rows(db, "SELECT official_closed_at FROM real_jobs") == [(None,)]
    imported = asyncio.run(import_fixture(db))
    assert imported.source_job_id == sibling
    assert queue(db).admit(imported.source_job_id).status == "queued"


def test_option_shaped_url_is_not_an_importer_command(db):
    intake = JobfeedIntake(db, ROOT / "jobfeed")
    result = asyncio.run(intake.add_job_url("--initialize"))
    assert result["status"] == "needs_details"
    assert result["reason"] == "Supply a public HTTP or HTTPS posting URL"
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]
    assert (
        rows(db, "SELECT name FROM sqlite_master WHERE name='application_queue'") == []
    )


@pytest.mark.parametrize("invalid", [None, URL, (), [], [URL] * 21, [URL, 123]])
def test_batch_shape_rejected_before_calls(db, monkeypatch, invalid):
    intake = JobfeedIntake(db, ROOT / "jobfeed")
    calls = []

    async def unexpected(url):
        calls.append(url)
        return {"status": "queued"}

    monkeypatch.setattr(intake, "add_job_url", unexpected)
    result = asyncio.run(intake.add_job_urls(invalid))
    assert result == {
        "status": "invalid_batch",
        "results": [],
        "counts_by_status": {},
        "newly_queued": 0,
        "reason": "Provide a list of 1 to 20 URL strings",
    }
    assert calls == []
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_batch_order_limits_and_isolates_item_exception(db, monkeypatch):
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def run():
        active = 0
        peak = 0
        completed = []
        all_started = asyncio.Event()
        release = asyncio.Event()
        item_two_completed = asyncio.Event()

        async def fake(url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                all_started.set()
            try:
                await release.wait()
                if url == "0":
                    await item_two_completed.wait()
                if url == "1":
                    raise OSError("sensitive fixture error must not be exposed")
                completed.append(url)
                if url == "2":
                    item_two_completed.set()
                return {"status": "queued" if url == "0" else "already_queued"}
            finally:
                active -= 1

        monkeypatch.setattr(intake, "add_job_url", fake)
        task = asyncio.create_task(intake.add_job_urls(["0", "1", "2", "3", "4", "5"]))
        await asyncio.wait_for(all_started.wait(), 2)
        assert peak == 3
        release.set()
        result = await task
        assert peak == 3
        assert completed[0] != "0"
        return result

    result = asyncio.run(run())
    assert [item["index"] for item in result["results"]] == list(range(6))
    assert [item["url"] for item in result["results"]] == ["0", "1", "2", "3", "4", "5"]
    assert result["results"][1]["result"]["status"] == "error"
    assert "sensitive" not in json.dumps(result)
    assert result["counts_by_status"] == {"queued": 1, "error": 1, "already_queued": 4}
    assert result["newly_queued"] == 1


def test_batch_overlap_and_separate_event_loops_converge(db, fixture_python):
    intake = JobfeedIntake(db, ROOT / "jobfeed", fixture_python)
    second = URL.replace("/123", "/124")

    async def overlap():
        return await asyncio.gather(
            intake.add_job_urls([URL, second, URL]),
            intake.add_job_urls([second, URL + "?utm_source=claude"]),
        )

    batches = asyncio.run(overlap())
    assert sum(batch["newly_queued"] for batch in batches) == 2
    assert rows(db, "SELECT count(*) FROM jobs") == [(2,)]
    assert rows(db, "SELECT count(*) FROM application_queue") == [(2,)]
    repeated = asyncio.run(intake.add_job_urls([URL, second, URL]))
    assert repeated["counts_by_status"] == {"already_queued": 3}
    assert repeated["newly_queued"] == 0


def test_batch_cancellation_reaps_children_and_does_not_start_waiters(db, tmp_path):
    launcher = tmp_path / "waiting-python"
    markers = tmp_path / "children"
    markers.mkdir()
    launcher.write_text(f"""#!{sys.executable}
import os,time,sys,runpy
from pathlib import Path
if '--url-keys' in sys.argv:
    module=sys.argv[2]
    sys.argv=[module,*sys.argv[3:]]
    runpy.run_module(module,run_name='__main__')
    sys.exit(0)
Path({str(markers)!r},str(os.getpid())).write_text('started')
time.sleep(300)
""")
    launcher.chmod(0o755)
    intake = JobfeedIntake(db, ROOT / "jobfeed", launcher)

    async def run():
        task = asyncio.create_task(
            intake.add_job_urls(
                [URL, URL.replace("/123", "/124"), URL.replace("/123", "/125")]
                + [URL] * 5
            )
        )
        try:
            async with asyncio.timeout(10):
                while len(list(markers.iterdir())) < 3:
                    await asyncio.sleep(0.01)
            assert len(list(markers.iterdir())) == 3
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(list(markers.iterdir())) == 3
    for marker in markers.iterdir():
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.name), 0)
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_stdio_batch_schema_mixed_inputs_and_retry(db, fixture_python):
    async def exercise():
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
                str(fixture_python),
            ],
            env={**os.environ, "PYTHONPATH": str(ROOT / "resume-application-pipeline")},
        )
        async with stdio_client(params) as (read, write_stream):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                tool = next(
                    tool
                    for tool in (await session.list_tools()).tools
                    if tool.name == "add_job_urls"
                )
                schema = tool.input_schema["properties"]["urls"]
                assert schema["type"] == "array"
                assert schema["items"]["type"] == "string"
                assert (schema["minItems"], schema["maxItems"]) == (1, 20)
                rejected = await session.call_tool("add_job_urls", {"urls": [URL] * 21})
                assert rejected.is_error
                assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]
                urls = [
                    URL,
                    URL.replace("/123", "/124"),
                    "--initialize",
                    URL + "?utm_source=claude",
                    URL,
                ]
                first = await session.call_tool("add_job_urls", {"urls": urls})
                assert not first.is_error
                first_result = json.loads(first.content[0].text)
                write(db, "UPDATE application_queue SET state='Cancelled'")
                repeated = await session.call_tool("add_job_urls", {"urls": urls})
                assert not repeated.is_error
                return urls, first_result, json.loads(repeated.content[0].text)

    urls, first, repeated = asyncio.run(exercise())
    assert [item["url"] for item in first["results"]] == urls
    assert [item["index"] for item in first["results"]] == [0, 1, 2, 3, 4]
    assert first["counts_by_status"] == {
        "queued": 2,
        "already_queued": 2,
        "needs_details": 1,
    }
    assert first["newly_queued"] == 2
    assert len((fixture_python.parent / "fetches.txt").read_text().splitlines()) == 2
    assert repeated["counts_by_status"] == {"already_queued": 4, "needs_details": 1}
    assert repeated["newly_queued"] == 0
    for item in repeated["results"]:
        if item["result"]["status"] == "already_queued":
            assert item["result"]["queue_state"] == "Cancelled"
    assert rows(db, "SELECT count(*) FROM jobs") == [(2,)]
    assert rows(
        db,
        "SELECT state,codex_application_session_id,submit_authorization FROM application_queue",
    ) == [("Cancelled", None, None), ("Cancelled", None, None)]


@pytest.mark.parametrize(
    "stored_url,requested",
    [
        (
            "https://careers.example.com/roles/42/?utm_source=legacy#description",
            "https://careers.example.com/roles/42?utm_source=claude",
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/123?gh_src=legacy",
            "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=claude",
        ),
    ],
)
def test_known_normalized_source_avoids_http_and_dns(db, stored_url, requested):
    async def run():
        store = SQLiteStore(db)
        await store.connect()
        try:
            saved = await store.save_job(
                JobPosting(
                    platform="official_search",
                    canonical_id="known42",
                    url=stored_url,
                    title="Software Engineer",
                    company="Acme",
                    location="New York",
                    jd_text=JD,
                    discovered_at=datetime.now(UTC),
                )
            )
        finally:
            await store.close()

        async def no_network(url):
            raise AssertionError("Known posting must not need DNS or HTTP")

        def no_http(request):
            raise AssertionError("Known posting must not be fetched")

        async with ImportHTTPClient(
            url_guard=no_network, transport=httpx.MockTransport(no_http)
        ) as client:
            imported = await JobUrlImportService(db, client=client).import_url(
                requested
            )
        assert imported.status == "existing"
        assert imported.source_job_id == int(saved.job_id)

    asyncio.run(run())


def test_native_apply_identity_reuses_tracked_scanned_source_without_http(db):
    async def run():
        store = SQLiteStore(db)
        await store.connect()
        try:
            saved = await store.save_job(
                JobPosting(
                    platform="linkedin",
                    canonical_id="987",
                    url="https://linkedin.com/jobs/view/987",
                    apply_url=URL + "?gh_src=scanned",
                    title="Software Engineer",
                    company="Acme",
                    location="New York",
                    jd_text=JD,
                    discovered_at=datetime.now(UTC),
                )
            )
        finally:
            await store.close()

        def no_http(request):
            raise AssertionError("Native application evidence must avoid HTTP")

        imported = await import_fixture(
            db, URL.replace("boards.", "job-boards."), no_http
        )
        assert imported.status == "existing"
        assert imported.source_job_id == int(saved.job_id)

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["title", "company", "jd_text"])
def test_incomplete_source_fetches_only_to_fill_missing_required_fields(db, missing):
    imported = asyncio.run(import_fixture(db))
    write(db, f'UPDATE jobs SET {missing}="" WHERE id=?', (imported.source_job_id,))
    calls = []

    def fetch(request):
        calls.append(str(request.url))
        return fixture_response(request)

    completed = asyncio.run(import_fixture(db, handler=fetch))
    assert completed.source_job_id == imported.source_job_id
    assert len(calls) == 1
    assert rows(db, "SELECT title,company,jd_text FROM jobs") == [
        ("Software Engineer", "Acme", JD)
    ]

    def forbidden(request):
        raise AssertionError("Completed posting must not be fetched twice")

    assert asyncio.run(import_fixture(db, handler=forbidden)).status == "existing"


def test_first_batch_native_aliases_fetch_each_unique_posting_once(db, fixture_python):
    intake = JobfeedIntake(db, ROOT / "jobfeed", fixture_python)
    urls = [
        URL,
        URL.replace("boards.", "job-boards."),
        URL + "?utm_source=claude",
        URL.replace("/123", "/124"),
    ]
    first = asyncio.run(intake.add_job_urls(urls))
    assert first["counts_by_status"] == {"queued": 2, "already_queued": 2}
    fetched = (fixture_python.parent / "fetches.txt").read_text().splitlines()
    assert len(fetched) == 2
    assert {httpx.URL(url).path.rsplit("/", 1)[-1] for url in fetched} == {"123", "124"}
    second = asyncio.run(intake.add_job_urls(urls))
    assert second["counts_by_status"] == {"already_queued": 4}
    assert (fixture_python.parent / "fetches.txt").read_text().splitlines() == fetched


def test_bad_preflight_response_does_not_start_imports(db, monkeypatch):
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def bad(*args):
        return {"status": "url_keys", "keys": ["only-one"]}

    async def forbidden(url):
        raise AssertionError("Invalid preflight cannot begin imports")

    monkeypatch.setattr(intake, "_run_importer", bad)
    monkeypatch.setattr(intake, "add_job_url", forbidden)
    result = asyncio.run(intake.add_job_urls([URL, URL]))
    assert result["counts_by_status"] == {"error": 2}
    assert result["newly_queued"] == 0


def test_url_lookup_detects_normalized_collision_alongside_native_match(db):
    asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_sibling(db))
    write(db, "INSERT INTO real_jobs DEFAULT VALUES")
    other_parent = rows(db, "SELECT max(id) FROM real_jobs")[0][0]
    write(
        db,
        "UPDATE jobs SET real_job_id=?,url=?,apply_url=NULL,external_identity=NULL WHERE id=?",
        (other_parent, URL + "?utm_source=legacy", sibling),
    )

    def forbidden(request):
        raise AssertionError("Ambiguous stored URL must not fetch")

    result = asyncio.run(import_fixture(db, handler=forbidden))
    assert result.status == "identity_review"


def test_metadata_completion_preserves_nonmissing_title_and_description(db):
    imported = asyncio.run(import_fixture(db))
    write(db, 'UPDATE jobs SET company="" WHERE id=?', (imported.source_job_id,))

    def response(request):
        return httpx.Response(
            200,
            json={
                "id": 123,
                "title": "Changed API title",
                "company_name": "Acme",
                "absolute_url": URL,
                "content": "Changed API description",
            },
        )

    result = asyncio.run(import_fixture(db, handler=response))
    assert result.source_job_id == imported.source_job_id
    assert rows(db, "SELECT title,company,jd_text FROM jobs") == [
        ("Software Engineer", "Acme", JD)
    ]


def test_incomplete_scanned_source_keeps_its_source_url(db):
    source = asyncio.run(save_sibling(db))
    write(db, 'UPDATE jobs SET title="" WHERE id=?', (source,))
    result = asyncio.run(import_fixture(db))
    assert result.source_job_id == source
    assert rows(db, "SELECT url,apply_url,title FROM jobs") == [
        ("https://linkedin.com/jobs/view/987", URL, "Software Engineer")
    ]


@pytest.mark.parametrize("receipt", ["legacy", "source", "source_applied"])
def test_distinct_portal_applied_flags_and_exact_source_blocks(db, receipt):
    imported = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_distinct_portal(db))
    if receipt == "legacy":
        write(db, "INSERT INTO applied(job_id,applied_at) VALUES(?,?)", (sibling, "2026-10-20"))
    else:
        column = "source_job_id" if receipt == "source" else "source_applied_job_id"
        write(db, f"INSERT INTO real_job_applications(real_job_id,{column},applied_at) VALUES(?,?,?)",
              (imported.real_job_id, sibling, "2026-10-20"))
    result = queue(db).admit(imported.source_job_id)
    assert result.status == "queued"
    assert result.related_source_job_ids == (sibling,)
    assert "related_source_applied" in result.identity_warnings
    assert queue(db).admit(sibling).status == "already_applied"


def test_distinct_portal_queue_state_is_preserved_and_flagged(db):
    imported = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_distinct_portal(db))
    q = queue(db)
    q.enqueue(sibling)
    write(db, "UPDATE application_queue SET state='SubmissionUnknown' WHERE job_id=?", (sibling,))
    result = q.admit(imported.source_job_id)
    assert result.status == "queued"
    assert result.queue_job_id == imported.source_job_id
    assert result.related_source_job_ids == (sibling,)
    assert "related_source_queued" in result.identity_warnings
    assert q.admit(imported.source_job_id).identity_warnings == result.identity_warnings
    assert rows(db, "SELECT state FROM application_queue WHERE job_id=?", (sibling,)) == [("SubmissionUnknown",)]


def test_missing_canonical_record_is_structural_error(db):
    imported = asyncio.run(import_fixture(db))
    write(db, "UPDATE jobs SET real_job_id=NULL WHERE id=?", (imported.source_job_id,))
    result = queue(db).admit(imported.source_job_id)
    assert result.status == "needs_details"
    assert result.reason == "Canonical job record is missing"


def test_stdio_mcp_surfaces_cross_source_warnings_in_single_and_batch(db, fixture_python):
    imported = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_distinct_portal(db))
    queue(db).enqueue(sibling)
    write(db, "UPDATE real_jobs SET identity_review_state='identity_conflict'")

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "pipeline.jobfeed_intake_mcp", "--db", str(db),
            "--jobfeed-root", str(ROOT / "jobfeed"), "--jobfeed-python", str(fixture_python),
        ], env={**os.environ, "PYTHONPATH": str(ROOT / "resume-application-pipeline")})
        async with stdio_client(params) as (read, output):
            async with ClientSession(read, output) as session:
                await session.initialize()
                single = await session.call_tool("add_job_url", {"url": URL})
                batch = await session.call_tool("add_job_urls", {"urls": [URL, URL]})
                assert not single.is_error and not batch.is_error
                return json.loads(single.content[0].text), json.loads(batch.content[0].text)

    single, batch = asyncio.run(exercise())
    assert single["status"] == "queued"
    assert single["source_job_id"] == imported.source_job_id
    assert single["related_source_job_ids"] == [sibling]
    assert single["identity_warnings"] == ["related_sources", "related_source_queued", "identity_review:identity_conflict"]
    for item in batch["results"]:
        assert item["result"]["status"] == "already_queued"
        assert item["result"]["identity_warnings"] == single["identity_warnings"]
        assert item["result"]["related_source_job_ids"] == [sibling]
    assert rows(db, "SELECT count(*) FROM application_queue") == [(2,)]


def test_real_store_records_separate_portal_receipts(db):
    from jobfeed.domain.models_application import ApplicationRecord
    from pipeline.jobfeed import Jobfeed

    imported = asyncio.run(import_fixture(db))
    sibling = asyncio.run(save_distinct_portal(db))
    feed = Jobfeed(db, ROOT / "jobfeed")

    async def record(source_id, url):
        store = SQLiteStore(db)
        await store.connect()
        try:
            return await store.record_real_job_application_with_snapshots(
                ApplicationRecord(job_id=str(source_id), applied_at=datetime.now(UTC)),
                real_job_id=str(imported.real_job_id), source_job_id=str(source_id), apply_url=url,
            )
        finally:
            await store.close()

    assert asyncio.run(record(imported.source_job_id, URL)) is True
    assert feed.is_applied(imported.source_job_id)
    assert not feed.is_applied(sibling)
    result = queue(db).admit(sibling)
    assert result.status == "queued"
    assert "related_source_applied" in result.identity_warnings
    assert asyncio.run(record(sibling, "https://linkedin.com/jobs/view/987")) is True
    assert feed.is_applied(sibling)
    assert asyncio.run(record(sibling, "https://linkedin.com/jobs/view/987")) is False
    assert rows(db, "SELECT source_job_id FROM real_job_applications ORDER BY id") == [(imported.source_job_id,), (sibling,)]
