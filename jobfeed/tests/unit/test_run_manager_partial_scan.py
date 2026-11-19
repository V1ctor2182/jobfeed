"""Source failures retain usable scan output without bypassing run ownership."""

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from jobfeed.domain.errors import RunLeaseLostError
from jobfeed.domain.models import PipelineRun
from jobfeed.services.run_manager import RunManager
from jobfeed.services.run_orchestration import RunLeaseSession
from tests.unit.test_run_manager import (
    FakeEvaluateService,
    RecordingLogger,
    RecordingStore,
)

SOURCE_FAILURE = "Scan has failed source work; inspect source progress"


class ScopeStore(RecordingStore):
    def __init__(self, *, owns_lease: bool = True) -> None:
        super().__init__()
        self.owns_lease = owns_lease
        self.scope = json.dumps({"run_id": "older-scan", "job_ids": ["1"]})
        self.scope_writes = 0

    async def checkpoint_run_with_lease(
        self, _run: PipelineRun, **_kwargs: object
    ) -> bool:
        return self.owns_lease

    async def set_state(self, key: str, value: str) -> None:
        assert key == "latest_scan_inserted_job_ids"
        self.scope = value
        self.scope_writes += 1

    async def get_state(self, key: str) -> str:
        assert key == "latest_scan_inserted_job_ids"
        return self.scope


class PartialScan:
    def __init__(
        self, *, failure: BaseException | None = None, inserted: bool = True
    ) -> None:
        self.failure = failure or RuntimeError(SOURCE_FAILURE)
        self.inserted = inserted
        self.session: RunLeaseSession | None = None

    async def run(
        self,
        _sources: object,
        *,
        on_progress: object = None,  # noqa: ARG002
        lease_session: RunLeaseSession,
    ) -> PipelineRun:
        run = lease_session.run
        self.session = lease_session
        run.scan_inserted_job_ids = ["10", "11"] if self.inserted else []
        run.jobs_inserted = len(run.scan_inserted_job_ids)
        run.errors = 1
        run.scan_progress = {
            "linkedin_guest": {"phase": "completed"},
            "jobright": {"phase": "failed", "message": "Authentication failed"},
        }
        raise self.failure


@pytest.mark.asyncio
async def test_partial_scan_keeps_scope_enriches_successes_and_exposes_failure() -> (
    None
):
    store = ScopeStore()
    enriched: list[list[str]] = []
    evaluation_inputs: list[list[str]] = []

    async def enrich(run: PipelineRun, _specs: object, _progress: object) -> None:
        enriched.append(list(run.scan_inserted_job_ids))
        run.scan_source = "linkedin_guest"
        run.scan_phase = "enriching_job_descriptions"

    def evaluator(**kwargs: Any) -> FakeEvaluateService:
        evaluation_inputs.append(kwargs["job_ids"])
        return FakeEvaluateService()

    manager = RunManager(
        store=store,  # type: ignore[arg-type]
        logger=RecordingLogger(),  # type: ignore[arg-type]
        scan_service_factory=PartialScan,  # type: ignore[arg-type]
        evaluate_service_factory=evaluator,  # type: ignore[arg-type]
        post_scan_hook=enrich,
    )
    run_id = await manager.trigger_scan([])
    await manager._tasks[run_id]

    assert json.loads(store.scope) == {"run_id": run_id, "job_ids": ["10", "11"]}
    assert enriched == [["10", "11"]]
    run = await store.get_pipeline_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.failure_message == SOURCE_FAILURE
    assert run.failed_stage == "source"
    assert run.failed_source == "jobright"
    assert run.errors == 1
    assert run.scan_progress["jobright"]["message"] == "Authentication failed"

    evaluation = await manager.trigger_evaluate(scope="latest_scan")
    await manager._tasks[evaluation]
    assert evaluation_inputs == [["10", "11"]]


@pytest.mark.asyncio
async def test_hook_lease_loss_stops_progress_and_preserves_interruption() -> None:
    store = ScopeStore()
    scan = PartialScan()
    progressed: list[str] = []

    async def enrich(
        run: PipelineRun, _specs: object, progress: Callable[[PipelineRun], None]
    ) -> None:
        assert scan.session is not None
        scan.session._lost.set()
        with contextlib.suppress(RunLeaseLostError):
            progress(run)
            progressed.append("continued after lease loss")

    manager = RunManager(
        store=store,  # type: ignore[arg-type]
        logger=RecordingLogger(),  # type: ignore[arg-type]
        scan_service_factory=lambda: scan,  # type: ignore[arg-type,return-value]
        evaluate_service_factory=lambda **_kw: FakeEvaluateService(),  # type: ignore[arg-type]
        post_scan_hook=enrich,
    )
    run_id = await manager.trigger_scan([])
    await manager._tasks[run_id]
    assert progressed == []
    assert json.loads(store.scope) == {"run_id": run_id, "job_ids": ["10", "11"]}
    run = await store.get_pipeline_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.failure_code == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "inserted", "owns_lease", "expected_code"),
    [
        (asyncio.CancelledError(), True, True, "user_stopped"),
        (RuntimeError("Storage failed"), True, True, "runtime_error"),
        (RuntimeError(SOURCE_FAILURE), True, False, "interrupted"),
        (RuntimeError(SOURCE_FAILURE), False, True, "runtime_error"),
    ],
)
async def test_incomplete_or_unowned_scan_cannot_publish_or_enrich(
    failure: BaseException, inserted: bool, owns_lease: bool, expected_code: str
) -> None:
    store = ScopeStore(owns_lease=owns_lease)
    enriched: list[str] = []

    async def enrich(run: PipelineRun, _specs: object, _progress: object) -> None:
        enriched.append(run.run_id)

    manager = RunManager(
        store=store,  # type: ignore[arg-type]
        logger=RecordingLogger(),  # type: ignore[arg-type]
        scan_service_factory=lambda: PartialScan(  # type: ignore[arg-type,return-value]
            failure=failure, inserted=inserted
        ),
        evaluate_service_factory=lambda **_kw: FakeEvaluateService(),  # type: ignore[arg-type]
        post_scan_hook=enrich,
    )
    run_id = await manager.trigger_scan([])
    await asyncio.gather(manager._tasks[run_id], return_exceptions=True)
    assert json.loads(store.scope) == {"run_id": "older-scan", "job_ids": ["1"]}
    assert store.scope_writes == 0
    assert enriched == []
    run = await store.get_pipeline_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.failure_code == expected_code
