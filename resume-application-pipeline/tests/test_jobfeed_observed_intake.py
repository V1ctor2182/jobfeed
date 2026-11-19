import asyncio
import json

import pytest

from jobfeed.adapters.url_import import JobUrlImportService
from pipeline.jobfeed_intake import JobfeedIntake
from test_jobfeed_intake import ROOT, rows, write
from test_jobfeed_intake import db as db
from test_jobfeed_intake_cli import invoke


def observation(**updates):
    return {
        "url": "https://www.linkedin.com/jobs/view/123456/",
        "native_job_id": "123456",
        "title": "Software Engineer",
        "company": "Example Company",
        "location": "New York",
        "jd_text": "Build accessible software. Requirements include Python and SQL.",
        "observed_at": "2026-10-23T12:00:00Z",
        "browser_id": "test-browser",
        "tab_id": "test-tab",
        "easy_apply_label": "Easy Apply to Software Engineer at Example Company",
        "page_snapshot": (
            '- heading "Software Engineer" [level=1]:\n'
            '  - link "Software Engineer":\n'
            '    - /url: /jobs/view/123456/?alternateChannel=search\n'
            '- button "Easy Apply to Software Engineer at Example Company":'
        ),
        **updates,
    }


def run_observation(db, payload):
    return invoke("--db", db, "--observed-posting", "-", stdin=json.dumps(payload))


def test_observed_cli_preserves_native_identity_and_queue_state(db):
    first = run_observation(db, observation())
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["counts_by_status"] == {"queued": 1}
    assert rows(db, "SELECT platform,canonical_id,url,apply_url FROM jobs") == [
        ("linkedin", "123456", "https://www.linkedin.com/jobs/view/123456", None)
    ]
    assert rows(db, "SELECT manual_priority,state,submit_authorization FROM application_queue") == [
        (1, "Queued", None)
    ]
    write(db, "UPDATE application_queue SET state='Cancelled'")
    repeated = run_observation(db, observation(url="https://linkedin.com/jobs/view/123456?utm_source=test"))
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["counts_by_status"] == {"already_queued": 1}
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(db, "SELECT state FROM application_queue") == [("Cancelled",)]


@pytest.mark.parametrize("updates", [
    {"native_job_id": "987654"},
    {"native_job_id": 123456},
    {"url": "https://www.linkedin.com.evil.example/jobs/view/123456"},
    {"url": "https://localhost/jobs/view/123456"},
    {"url": "https://www.linkedin.com/jobs/search/?currentJobId=123456"},
    {"url": "http://www.linkedin.com/jobs/view/123456"},
    {"company": ""},
    {"title": ""},
    {"location": ""},
    {"jd_text": " "},
    {"observed_at": "2026-10-23T12:00:00"},
    {"observed_at": "2099-10-23T12:00:00Z"},
    {"easy_apply_label": "Apply"},
    {"page_snapshot": "An external Apply button"},
    {"browser_id": ""},
    {"tab_id": ""},
])
def test_observed_cli_rejects_invalid_snapshot_before_any_write(db, updates):
    result = run_observation(db, observation(**updates))
    assert result.returncode == 2
    assert result.stdout == ""
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_observed_service_validates_before_persistence(db):
    result = asyncio.run(JobUrlImportService(db).import_observed_posting(observation(native_job_id="7")))
    assert result.status == "needs_details"
    assert result.source_job_id is None
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


@pytest.mark.parametrize("snapshot", [
    observation()["page_snapshot"].replace("/jobs/view/123456/", "/jobs/view/654321/"),
    '- link "Software Engineer":\n  - /url: /jobs/view/123456/\n'
    '- button "Easy Apply to Software Engineer at Example Company":',
    '- link "Software Engineer":\n  - /url: /jobs/view/123456/\n'
    + observation()["page_snapshot"].replace("/jobs/view/123456/", "/jobs/view/654321/"),
])
def test_snapshot_must_bind_identity_to_detail_heading(db, snapshot):
    result = run_observation(db, observation(page_snapshot=snapshot))
    assert result.returncode == 2
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_observed_cli_preserves_already_applied_boundary(db):
    result = asyncio.run(JobUrlImportService(db).import_observed_posting(observation()))
    write(db, "INSERT INTO applied(job_id,applied_at) VALUES(?,?)",
          (result.source_job_id, "2026-10-23"))
    repeated = run_observation(db, observation())
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["counts_by_status"] == {"already_applied": 1}
    assert rows(db, "SELECT count(*) FROM application_queue") == [(0,)]


@pytest.mark.parametrize("import_result", [
    {"status": "error", "reason": "Save outcome is unknown"},
    {"status": "imported"},
])
def test_unknown_observed_import_does_not_admit_queue(db, monkeypatch, import_result):
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def unknown(*args):
        return import_result

    monkeypatch.setattr(intake, "_run_importer", unknown)
    result = asyncio.run(intake.add_observed_posting(observation()))
    assert result["status"] == "error"
    assert "unknown" in result["reason"]
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]
    assert rows(db, "SELECT name FROM sqlite_master WHERE name='application_queue'") == []


def test_observed_cli_refuses_other_input_sources(db):
    result = invoke("--db", db, "--observed-posting", "-", observation()["url"], stdin=json.dumps(observation()))
    assert result.returncode == 2
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_observed_import_never_fetches_and_preserves_closed_source(db, monkeypatch):
    service = JobUrlImportService(db)

    async def unexpected_fetch(*args):
        pytest.fail("Browser-observed intake must not fetch a remote posting")

    monkeypatch.setattr(service, "_fetch", unexpected_fetch)
    first = asyncio.run(service.import_observed_posting(observation()))
    assert first.status == "imported"
    write(db, "UPDATE jobs SET closed_at='2026-10-23T12:00:00Z'")
    repeated = asyncio.run(service.import_observed_posting(observation()))
    assert repeated.status == "closed"
    assert repeated.source_job_id == first.source_job_id
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
