"""Stage B sweep: retry failed Stage B jobs with the sweep model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jobfeed.domain.models import JobPosting, PipelineRun
from jobfeed.services._evaluate_claims import release_stage_b_for_run
from jobfeed.services._evaluate_helpers import load_stage_a_scores, require_job_id
from jobfeed.services.evaluate_types import EvaluateDependencies

ScoreStageB = Callable[..., Awaitable[str]]


async def sweep_stage_b(
    deps: EvaluateDependencies,
    run: PipelineRun,
    failed: list[tuple[JobPosting, str]],
    *,
    score_stage_b: ScoreStageB,
) -> None:
    """Retry failed Stage B jobs with the sweep model, else record errors.

    A job whose last LLM call raised ("unavailable") is released without an
    error, so a backend outage does not use up its retries.

    Args:
        deps: Evaluation dependencies (sweep LLM + store).
        run: Pipeline run whose error counter is updated.
        failed: Jobs whose primary Stage B pass failed, with that outcome.
        score_stage_b: Bound scorer reused for the sweep pass.
    """
    sweep = deps.llm_stage_b_sweep
    error = "stage_b_failed_no_sweep" if sweep is None else "stage_b_sweep_failed"
    stage_a_scores = (
        {}
        if sweep is None
        else await load_stage_a_scores(deps.store, [job for job, _ in failed])
    )
    for job, outcome in failed:
        jid = require_job_id(job)
        final = (
            outcome
            if sweep is None
            else await score_stage_b(
                job,
                run,
                sweep,
                stage_a_score=stage_a_scores.get(jid),
                parse_attempts=1,
            )
        )
        if final == "completed":
            continue
        run.errors += 1
        if final == "unavailable":
            await release_stage_b_for_run(deps.store, jid)
        else:
            await deps.store.save_stage_b_error(jid, error)
