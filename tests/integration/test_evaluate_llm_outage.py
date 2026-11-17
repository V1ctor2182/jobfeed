"""An LLM backend outage fails the run, not the jobs it touched."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfeed.adapters.llm._prompts import JinjaPromptRenderer
from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.domain.errors import LLMRuntimeUnavailable
from jobfeed.domain.models import JobPosting, LLMRequest, LLMResponse, QualityBand
from jobfeed.domain.scoring import MAX_STAGE_RETRIES
from jobfeed.services.evaluate import LLM_OUTAGE_STREAK_LIMIT, EvaluateService
from jobfeed.services.evaluate_types import EvaluateDependencies
from tests.integration.test_canonical_evaluate_service import _PaidFake
from tests.unit.test_evaluate_lease_scheduling import _LeaseProbe, _service


class _Backend(_PaidFake):
    """Scores normally, except one stage follows a per-job plan of failures."""

    def __init__(self, stage: str, failure: str, plan: list[bool]) -> None:
        super().__init__()
        self.model = "mock-" + stage
        self.failure = failure
        self.plan = plan
        self.decisions: dict[str, bool] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.model == self.model:
            key = str(request.messages)
            if key not in self.decisions:
                self.decisions[key] = self.plan.pop(0) if self.plan else False
            if self.decisions[key] and self.failure == "runtime":
                raise RuntimeError("claude-cli exited with status 1")
            if self.decisions[key]:
                response = await super().complete(request)
                response.content = "not json"
                return response
        return await super().complete(request)


async def _jobs(store: SQLiteStore, count: int) -> list[str]:
    ids = []
    for index in range(count):
        saved = await store.save_job(
            JobPosting(
                platform="linkedin",
                canonical_id=f"outage-{index}",
                url=f"https://example.test/outage/{index}",
                title=f"Engineer {index}",
                company=f"Acme {index}",
                location="Remote",
                discovered_at=datetime.now(UTC),
                jd_text=f"Build software services for team {index}. " * 30,
                jd_quality=QualityBand.FULL,
            )
        )
        ids.append(saved.job_id)
    return ids


def _evaluator(
    store: SQLiteStore, llm: _Backend, *, sweep: bool = False
) -> EvaluateService:
    template, _, _ = _service(_LeaseProbe())
    return EvaluateService(
        deps=EvaluateDependencies(
            store=store,
            store_ops=store,
            store_status=store,
            prompt_renderer=JinjaPromptRenderer(Path("src/jobfeed/templates")),
            llm_stage_a=llm,
            llm_stage_b=llm,
            llm_stage_b_sweep=llm if sweep else None,
        ),
        config=template._config,
        logger=template._logger,
    )


def _scored(run, stage: str) -> int:
    return run.stage_a_scored if stage == "a" else run.stage_b_scored


@pytest.mark.parametrize("sweep", [False, True])
@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("stage", ["a", "b"])
@pytest.mark.parametrize(
    ("failure", "still_scored"), [("runtime", True), ("parse", False)]
)
async def test_only_parse_failures_count_against_the_job(  # noqa: PLR0913
    tmp_path, canonical, stage, failure, still_scored, sweep
):
    store = SQLiteStore(tmp_path / "outage.sqlite")
    await store.connect()
    try:
        [job_id] = await _jobs(store, 1)
        llm = _Backend(stage, failure, [])
        service = _evaluator(store, llm, sweep=sweep)
        for _ in range(MAX_STAGE_RETRIES):
            llm.decisions.clear()
            llm.plan = [True]
            run = await service.run(
                stage="both", limit=1, job_ids=[job_id], canonical=canonical
            )
            assert run.errors == 1
        llm.decisions.clear()
        run = await service.run(
            stage="both", limit=1, job_ids=[job_id], canonical=canonical
        )
        assert _scored(run, stage) == int(still_scored)
    finally:
        await store.close()


@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("stage", ["a", "b"])
async def test_three_consecutive_outages_stop_the_run(tmp_path, canonical, stage):
    store = SQLiteStore(tmp_path / "outage.sqlite")
    await store.connect()
    try:
        ids = await _jobs(store, 5)
        llm = _Backend(stage, "runtime", [True] * 5)
        service = _evaluator(store, llm)
        with pytest.raises(LLMRuntimeUnavailable, match="LLM backend unavailable"):
            await service.run(stage="both", limit=5, job_ids=ids, canonical=canonical)
        assert len(llm.decisions) == LLM_OUTAGE_STREAK_LIMIT
        [failed_run], _ = await store.list_pipeline_runs(limit=1)
        assert failed_run.status == "failed"
        assert "LLM backend unavailable" in (failed_run.failure_message or "")

        llm.plan = []
        llm.decisions.clear()
        run = await service.run(stage="both", limit=5, job_ids=ids, canonical=canonical)
        assert _scored(run, stage) == len(ids)
    finally:
        await store.close()


@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("stage", ["a", "b"])
async def test_a_response_resets_the_outage_streak(tmp_path, canonical, stage):
    store = SQLiteStore(tmp_path / "outage.sqlite")
    await store.connect()
    try:
        plan = [True, True, False, True, True]
        ids = await _jobs(store, len(plan))
        llm = _Backend(stage, "runtime", list(plan))
        service = _evaluator(store, llm)
        run = await service.run(stage="both", limit=5, job_ids=ids, canonical=canonical)
        assert run.status == "succeeded"
        assert run.errors == plan.count(True)
        assert _scored(run, stage) == 1
    finally:
        await store.close()
