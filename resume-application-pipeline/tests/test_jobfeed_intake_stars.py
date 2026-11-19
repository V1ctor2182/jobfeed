import asyncio
import sqlite3
from dataclasses import asdict

import pytest
from test_jobfeed_intake import ROOT, URL, import_fixture, rows, write
from test_jobfeed_intake import db as db  # noqa: PLC0414
from test_jobfeed_intake import fixture_python as fixture_python  # noqa: PLC0414

from pipeline.jobfeed_intake import JobfeedIntake
from pipeline.store import ApplicationQueue


@pytest.mark.parametrize("queue_status", ["queued", "closed", "already_applied"])
def test_manual_intake_defaults_star_independently_of_queue(
    db, fixture_python, queue_status
):
    imported = asyncio.run(import_fixture(db))
    if queue_status == "closed":
        write(db, "UPDATE jobs SET closed_at='2026-10-23T12:00:00Z'")
    elif queue_status == "already_applied":
        write(
            db,
            "INSERT INTO applied(job_id,applied_at) VALUES(?,?)",
            (imported.source_job_id, "2026-10-23"),
        )
    result = asyncio.run(
        JobfeedIntake(db, ROOT / "jobfeed", fixture_python).add_job_url(URL)
    )
    assert result["status"] == queue_status
    assert result["starred"] is True
    assert "star_warning" not in result
    if queue_status == "closed":
        assert rows(
            db, "SELECT name FROM sqlite_master WHERE name='application_queue'"
        ) == []
    else:
        expected_count = 1 if queue_status == "queued" else 0
        assert rows(db, "SELECT count(*) FROM application_queue") == [(expected_count,)]
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]


def test_star_finishes_before_queue_failure(db, fixture_python, monkeypatch):
    intake = JobfeedIntake(db, ROOT / "jobfeed", fixture_python)
    calls = []
    run_importer = intake._run_importer

    async def observed(arguments, payload=None):
        result = await run_importer(arguments, payload)
        calls.append(arguments)
        return result

    def failed(*args, **kwargs):
        assert calls[-1] == ["--star-imported", "1"]
        raise sqlite3.OperationalError("synthetic queue failure")

    monkeypatch.setattr(intake, "_run_importer", observed)
    monkeypatch.setattr(ApplicationQueue, "admit", failed)
    result = asyncio.run(intake.add_job_url(URL))
    assert result["status"] == "imported_not_queued"
    assert result["source_job_id"] == 1
    assert result["starred"] is True
    assert calls == [["--", URL], ["--star-imported", "1"]]
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]


def test_prior_user_unstar_from_helper_is_preserved(db, monkeypatch):
    imported = asyncio.run(import_fixture(db))
    intake = JobfeedIntake(db, ROOT / "jobfeed")
    calls = []

    async def importer(arguments, payload=None):
        calls.append(arguments)
        if arguments == ["--", URL]:
            return asdict(imported)
        return {
            "status": "starred",
            "source_job_id": imported.source_job_id,
            "starred": False,
        }

    monkeypatch.setattr(intake, "_run_importer", importer)
    result = asyncio.run(intake.add_job_url(URL))
    assert result["status"] == "queued"
    assert result["starred"] is False
    assert "star_warning" not in result
    assert calls == [["--", URL], ["--star-imported", str(imported.source_job_id)]]


@pytest.mark.parametrize(
    "star_result",
    [
        {"status": "error"},
        {"status": "starred", "source_job_id": 999, "starred": True},
        {"status": "starred", "source_job_id": True, "starred": True},
        {"status": "starred", "source_job_id": 1, "starred": "true"},
        RuntimeError("synthetic importer failure"),
    ],
)
def test_star_failure_reports_unknown_without_reimport_or_blocking_queue(
    db, monkeypatch, star_result
):
    imported = asyncio.run(import_fixture(db))
    intake = JobfeedIntake(db, ROOT / "jobfeed")
    calls = []

    async def importer(arguments, payload=None):
        calls.append(arguments)
        if arguments == ["--", URL]:
            return asdict(imported)
        if isinstance(star_result, Exception):
            raise star_result
        return star_result

    monkeypatch.setattr(intake, "_run_importer", importer)
    result = asyncio.run(intake.add_job_url(URL))
    assert result["status"] == "queued"
    assert result["starred"] is None
    assert "star update could not be verified" in result["star_warning"]
    assert calls == [["--", URL], ["--star-imported", str(imported.source_job_id)]]
    assert rows(db, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(db, "SELECT state FROM application_queue") == [("Queued",)]


@pytest.mark.parametrize("status", ["error", "needs_details", "identity_review"])
def test_failed_import_never_stars(db, monkeypatch, status):
    calls = []
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def importer(arguments, payload=None):
        calls.append(arguments)
        return {"status": status, "source_job_id": 1}

    monkeypatch.setattr(intake, "_run_importer", importer)
    result = asyncio.run(intake.add_job_url(URL))
    assert result["status"] == status
    assert "starred" not in result
    assert calls == [["--", URL]]
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


@pytest.mark.parametrize("source_id", [None, 0, -1, True, "1"])
def test_invalid_persisted_source_id_never_stars(db, monkeypatch, source_id):
    calls = []
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def importer(arguments, payload=None):
        calls.append(arguments)
        return {"status": "imported", "source_job_id": source_id}

    monkeypatch.setattr(intake, "_run_importer", importer)
    result = asyncio.run(intake.add_job_url(URL))
    assert result["status"] == "error"
    assert "starred" not in result
    assert calls == [["--", URL]]


def test_closed_remote_posting_without_persisted_id_never_stars(db, monkeypatch):
    calls = []
    intake = JobfeedIntake(db, ROOT / "jobfeed")

    async def importer(arguments, payload=None):
        calls.append(arguments)
        return {"status": "closed", "source_job_id": None}

    monkeypatch.setattr(intake, "_run_importer", importer)
    result = asyncio.run(intake.add_job_url(URL))
    assert result == {"status": "closed", "source_job_id": None}
    assert calls == [["--", URL]]
