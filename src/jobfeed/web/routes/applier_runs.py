"""Read-only Claude Applier runs for the dashboard's Applier trace page."""

from __future__ import annotations

import asyncio
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response

from jobfeed.applier_trace import (
    ApplierTraceReader,
    JobLabel,
    build_trace,
    run_summary,
)
from jobfeed.ports.store import JobStore
from jobfeed.services.applier_trace_models import ApplierRuns, ApplierTrace
from jobfeed.web.deps import get_store
from jobfeed.web.errors import ApiError

router = APIRouter()

_Store = Annotated[JobStore, Depends(get_store)]


def _reader(request: Request) -> ApplierTraceReader:
    return cast(ApplierTraceReader, request.app.state.applier_traces)


async def _label(store: JobStore, job_id: int) -> JobLabel:
    job = await store.get_job(str(job_id))
    return JobLabel(job.company, job.title) if job else JobLabel(None, None)


@router.get("/applier-runs", response_model=ApplierRuns)
async def applier_runs(
    request: Request, response: Response, store: _Store
) -> ApplierRuns:
    """List recorded Applier runs, newest first.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.
        store: Store used to label each run with its job.

    Returns:
        A summary of each run, and whether the autopilot switch is on.
    """
    reader = _reader(request)
    response.headers["Cache-Control"] = "no-store"
    launches = await asyncio.to_thread(reader.launches)
    counts = await asyncio.to_thread(
        lambda: [len(reader.transcripts(launch)) for launch in launches]
    )
    return ApplierRuns(
        runs=[
            run_summary(launch, await _label(store, launch.job_id), count)
            for launch, count in zip(launches, counts, strict=True)
        ],
        autopilot_enabled=await asyncio.to_thread(reader.autopilot_enabled),
    )


@router.get("/applier-runs/{job_id}/trace", response_model=ApplierTrace)
async def applier_trace(
    job_id: int, request: Request, response: Response, store: _Store
) -> ApplierTrace:
    """Return the trace of a job's newest Applier run.

    Args:
        job_id: Job whose run should be traced.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.
        store: Store used to label the run with its job.

    Returns:
        The run's trace tree.

    Raises:
        ApiError: 404 if the job has no recorded run.
    """
    reader = _reader(request)
    response.headers["Cache-Control"] = "no-store"
    launches = await asyncio.to_thread(reader.launches, job_id)
    if not launches:
        raise ApiError(
            404, "applier_run_not_found", f"No Applier run for job {job_id}."
        )
    sessions = await asyncio.to_thread(reader.sessions, launches[0])
    return build_trace(launches[0], sessions, await _label(store, job_id), reader.now())
