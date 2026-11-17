"""Submission writes require artifacts bound to the stored posting."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.apply_health import ApplyHealthReader
from jobfeed.cli.apply import apply_cmd
from jobfeed.config import Settings
from jobfeed.services.application import ApplicationService, ApplyRequest
from jobfeed.submission_evidence import EvidenceValidationError
from jobfeed.web.deps import get_application_service, get_store
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.applications import router
from tests.support.confirmation import confirmation_folder
from tests.support.factories import make_job
from tests.unit.test_cli_apply import _make_app, _make_store


@pytest.mark.parametrize(
    "defect",
    ["missing", "prose", "artifact_missing", "wrong_job", "wrong_url", "method_bypass"],
)
async def test_unverified_submission_cannot_write(tmp_path: Path, defect: str) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job(url="https://careers.example/jobs/42"))
        folder = confirmation_folder(tmp_path / "evidence", int(saved.job_id))
        receipt_file = folder / "submission-receipt.json"
        receipt = json.loads(receipt_file.read_text())
        if defect == "prose":
            receipt.pop("confirmation")
            receipt["success_evidence"] = "I submitted the application."
        elif defect == "artifact_missing":
            (folder / "confirmation.eml").unlink()
        elif defect == "wrong_job":
            receipt["job_id"] = 999
        elif defect == "wrong_url":
            receipt["job_url"] = "https://careers.example/jobs/unrelated"
        receipt_file.write_text(json.dumps(receipt))
        svc = ApplicationService(store, MagicMock())
        req = ApplyRequest(
            job_id=saved.job_id,
            master_resume="Synthetic resume",
            confirmation_folder=None
            if defect in {"missing", "method_bypass"}
            else folder,
            application_method="history" if defect == "method_bypass" else "web",
        )
        with pytest.raises(EvidenceValidationError):
            await svc.apply(req)
        assert await store.get_application(saved.job_id) is None
        assert await store.list_resume_snapshots() == []
    finally:
        await store.close()


async def test_confirmation_email_records_once_and_preserves_history(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job(url="https://careers.example/jobs/42"))
        folder = confirmation_folder(tmp_path / "evidence", int(saved.job_id))
        svc = ApplicationService(store, MagicMock())
        req = ApplyRequest(
            job_id=saved.job_id,
            master_resume="Synthetic resume",
            confirmation_folder=folder,
        )
        assert await svc.apply(req) is True
        before = await store.get_application(saved.job_id)
        assert before is not None
        assert (
            await svc.apply(
                replace(req, confirmation_folder=None, master_resume="Changed")
            )
            is False
        )
        assert await store.get_application(saved.job_id) == before
    finally:
        await store.close()


@pytest.mark.parametrize("canonical", [False, True])
async def test_web_cannot_supply_local_evidence_path(
    tmp_path: Path, canonical: bool
) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job(url="https://careers.example/jobs/42"))
        folder = confirmation_folder(tmp_path / "evidence", int(saved.job_id))
        svc = ApplicationService(store, MagicMock())
        app = FastAPI()
        app.include_router(router)
        install_error_handling(app)
        app.dependency_overrides[get_application_service] = lambda: svc
        app.dependency_overrides[get_store] = lambda: store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            real_id = await store.resolve_real_job_id(saved.job_id)
            path = (
                f"/real-jobs/{real_id}/apply"
                if canonical
                else f"/jobs/{saved.job_id}/apply"
            )
            response = await client.post(
                path,
                data={
                    "confirmation_folder": str(folder),
                    "method": "history",
                    "source_job_id": saved.job_id,
                },
            )
            assert response.status_code == httpx.codes.CONFLICT
            assert response.json()["error"]["code"] == "confirmation_required"
        assert await store.get_application(saved.job_id) is None
    finally:
        await store.close()


def test_cli_missing_evidence_fails_without_record(tmp_path: Path) -> None:
    store = _make_store()
    resume = tmp_path / "resume.md"
    resume.write_text("Synthetic resume")
    result = CliRunner().invoke(
        apply_cmd,
        ["42", "--method", "email", "--notes", "Application received"],
        obj=_make_app(store, resume),
    )
    assert result.exit_code != 0
    assert "Confirmation evidence is required" in result.output
    store.record_application_with_snapshots.assert_not_awaited()


@pytest.mark.parametrize("binding", ["missing", "other_folder", "registered"])
def test_cli_requires_dashboard_discoverable_evidence(
    tmp_path: Path, monkeypatch, binding: str
) -> None:
    db_path = tmp_path / "jobs.sqlite"
    runtime = tmp_path / "pipeline"
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(runtime))
    store = SQLiteStore(db_path)

    async def seed() -> str:
        await store.connect()
        saved = await store.save_job(make_job(url="https://careers.example/jobs/42"))
        await store.close()
        return saved.job_id

    job_id = asyncio.run(seed())
    registered = confirmation_folder(runtime / "applications" / "job-one", int(job_id))
    provided = registered
    if binding == "other_folder":
        provided = confirmation_folder(
            runtime / "applications" / "unbound", int(job_id)
        )
    if binding != "missing":
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY, "
                "application_folder_relpath TEXT)"
            )
            db.execute(
                "INSERT INTO application_queue VALUES (?,?)", (int(job_id), "job-one")
            )
    resume = tmp_path / "resume.md"
    resume.write_text("Synthetic resume")
    app = _make_app(store, resume)
    app["settings"] = Settings.model_validate(
        {"db": {"path": str(db_path)}, "llm": {"master_resume_path": str(resume)}}
    )
    result = CliRunner().invoke(
        apply_cmd, [job_id, "--confirmation-folder", str(provided)], obj=app
    )

    async def records():
        await store.connect()
        try:
            return await store.list_real_job_applications()
        finally:
            await store.close()

    saved_records = asyncio.run(records())
    if binding == "registered":
        assert result.exit_code == 0, result.output
        assert len(saved_records) == 1
        evidence = ApplyHealthReader(root=runtime).confirmation(db_path, int(job_id))
        assert evidence.artifact_path == registered / "confirmation.eml"
    else:
        assert result.exit_code != 0
        assert "registered application folder" in result.output
        assert saved_records == []
