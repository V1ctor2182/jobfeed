"""Status changes cannot manufacture a submitted application."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from jobfeed.adapters.store.postgres import PostgresStore
from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.cli.status import mark
from jobfeed.domain.models import BulkTransitionRequest, TransitionRequest
from jobfeed.services.application import ApplicationService, ApplyRequest
from jobfeed.services.workflow import WorkflowService
from jobfeed.web.deps import get_store, get_workflow_service
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.real_jobs_workflow import router as canonical_router
from jobfeed.web.routes.workflow import router
from tests.support.confirmation import confirmation_folder
from tests.support.factories import make_job
from tests.unit.test_cli_apply import _make_app


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_status_and_bulk_require_existing_record(
    tmp_path: Path, canonical: bool, force: bool
) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job())
        job_id = (
            await store.resolve_real_job_id(saved.job_id) if canonical else saved.job_id
        )
        transition = (
            store.transition_real_job_status if canonical else store.transition_status
        )
        bulk = (
            store.transition_real_jobs_bulk
            if canonical
            else store.transition_status_bulk
        )
        status = store.get_real_job_status if canonical else store.get_status
        before = await status(job_id)
        with pytest.raises(ValueError, match="confirmation evidence"):
            await transition(
                TransitionRequest(
                    job_id=job_id, new_status="applied", force=force, i_mean_it=True
                )
            )
        result = await bulk(
            BulkTransitionRequest(
                items=[(job_id, "applied")],
                reason_selected="test",
                reason_cascade="test",
                force=force,
                i_mean_it=True,
            )
        )
        assert result.succeeded == 0
        assert len(result.failed) == 1
        assert await status(job_id) == before
    finally:
        await store.close()


@pytest.mark.parametrize("canonical", [False, True])
async def test_restore_cannot_create_submission_but_preserves_recorded_history(
    tmp_path: Path, canonical: bool
) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job(url="https://careers.example/jobs/42"))
        real_id = await store.resolve_real_job_id(saved.job_id)
        job_id = real_id if canonical else saved.job_id
        transition = (
            store.transition_real_job_status if canonical else store.transition_status
        )
        status = store.get_real_job_status if canonical else store.get_status
        workflow = WorkflowService(store, MagicMock())
        restore = store.restore_real_job if canonical else workflow.restore
        await transition(
            TransitionRequest(job_id=job_id, new_status="archived", force=True)
        )
        with sqlite3.connect(tmp_path / "jobs.sqlite") as db:
            db.execute(
                "DELETE FROM real_job_status_history"
                if canonical
                else "DELETE FROM job_status_history"
            )
        with pytest.raises(ValueError, match="confirmation evidence"):
            await restore(job_id)
        assert (await status(job_id)).status == "archived"
        await transition(
            TransitionRequest(
                job_id=job_id, new_status="new", force=True, i_mean_it=True
            )
        )
        svc = ApplicationService(store, MagicMock())
        folder = confirmation_folder(tmp_path / "evidence", int(saved.job_id))
        assert await svc.apply(
            ApplyRequest(
                job_id=saved.job_id,
                real_job_id=real_id if canonical else None,
                source_job_id=saved.job_id if canonical else None,
                master_resume="Synthetic",
                confirmation_folder=folder,
            )
        )
        await transition(
            TransitionRequest(job_id=job_id, new_status="archived", force=True)
        )
        assert await restore(job_id) == "applied"
    finally:
        await store.close()


async def test_web_status_routes_cannot_force_applied(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "jobs.sqlite")
    await store.connect()
    try:
        saved = await store.save_job(make_job())
        real_id = await store.resolve_real_job_id(saved.job_id)
        app = FastAPI()
        app.include_router(router)
        app.include_router(canonical_router)
        install_error_handling(app)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
            store, MagicMock()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in [
                f"/jobs/{saved.job_id}/transition",
                f"/real-jobs/{real_id}/transition",
            ]:
                response = await client.post(
                    path, json={"to": "applied", "force": True}
                )
                assert response.status_code == httpx.codes.CONFLICT, response.text
                assert "confirmation evidence" in response.text
        assert await store.get_application(saved.job_id) is None
    finally:
        await store.close()


def test_cli_cannot_force_applied(tmp_path: Path) -> None:

    store = SQLiteStore(tmp_path / "jobs.sqlite")

    async def seed() -> str:
        await store.connect()
        saved = await store.save_job(make_job())
        await store.close()
        return saved.job_id

    job_id = asyncio.run(seed())
    resume = tmp_path / "resume.md"
    resume.write_text("Synthetic")
    result = CliRunner().invoke(
        mark,
        [job_id, "--status", "applied", "--force", "--i-mean-it"],
        obj=_make_app(store, resume),
    )
    assert result.exit_code != 0
    assert "confirmation evidence" in result.output


async def test_postgres_status_guard_precedes_force_and_write() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = [{"status": "new"}, None]
    with pytest.raises(ValueError, match="confirmation evidence"):
        await PostgresStore._transition_status_in_tx(
            MagicMock(),
            conn,
            TransitionRequest(
                job_id="42", new_status="applied", force=True, i_mean_it=True
            ),
        )
    conn.execute.assert_not_awaited()
