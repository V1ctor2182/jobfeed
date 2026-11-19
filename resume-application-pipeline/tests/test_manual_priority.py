import asyncio
import sqlite3

from pipeline.jobfeed_intake import JobfeedIntake
from pipeline.lifecycle import ApplicationState as S
from pipeline.store import ApplicationQueue
from runtime_fixture import RuntimeFixture
from test_jobfeed_intake import db, fixture_python, URL, ROOT, rows


def test_intake_promotes_existing_without_fetch_or_reset(db, fixture_python):
    intake = JobfeedIntake(db, ROOT / 'jobfeed', fixture_python)
    first = asyncio.run(intake.add_job_url(URL))
    job_id = first['source_job_id']
    assert first['manual_priority'] is True
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE application_queue SET manual_priority=0,state='Cancelled',retry_count=4,submit_authorization='retained' WHERE job_id=?", (job_id,))
    second = asyncio.run(intake.add_job_url(URL + '?utm_source=again'))
    assert second['manual_priority'] is True
    assert second['status'] == 'already_queued'
    assert rows(db, "SELECT state,retry_count,submit_authorization,manual_priority FROM application_queue") == [('Cancelled', 4, 'retained', 1)]
    assert len(rows(db, 'SELECT id FROM jobs')) == 1
    assert len((fixture_python.parent / 'fetches.txt').read_text().splitlines()) == 1


def test_manual_queue_order_is_scoped_and_transition_preserves_priority(tmp_path):
    fixture = RuntimeFixture(tmp_path)
    queue = ApplicationQueue(fixture.db)
    queue.initialize()
    queue.admit(1)
    queue.admit(2, manual_priority=True)
    queue.admit(3, manual_priority=True)
    assert [job.job_id for job in queue.list_active()] == [2, 3, 1]
    assert queue.claim_queued([1, 2]).job_id == 2
    assert queue.get(2).manual_priority is True
    assert queue.get(3).state == S.QUEUED
    assert queue.claim_queued([1]).job_id == 1
    assert queue.claim_queued().job_id == 3


def test_exact_id_priority_uses_admission_and_rejects_unknown_ids(tmp_path):
    fixture = RuntimeFixture(tmp_path)
    intake = JobfeedIntake(fixture.db, tmp_path)
    result = intake.prioritize_jobs([1, 999, 1])
    assert [row['status'] for row in result['results']] == ['queued', 'needs_details', 'already_queued']
    assert [row['manual_priority'] for row in result['results']] == [True, False, True]
    assert intake.prioritize_jobs([True])['status'] == 'invalid_batch'


def test_simultaneous_manual_batches_fetch_once_and_keep_all_priority(db, fixture_python):
    intake = JobfeedIntake(db, ROOT / 'jobfeed', fixture_python)

    async def import_batches():
        return await asyncio.gather(
            intake.add_job_urls([URL, URL.replace('/123', '/124')]),
            intake.add_job_urls([URL + '?utm_source=second', URL.replace('/123', '/124')]),
        )

    batches = asyncio.run(import_batches())
    assert sum(batch['newly_queued'] for batch in batches) == 2
    assert rows(db, 'SELECT manual_priority FROM application_queue ORDER BY job_id') == [(1,), (1,)]
    assert len((fixture_python.parent / 'fetches.txt').read_text().splitlines()) == 2


