"""Historical scan new-job attribution routes: source counts and postings."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from jobfeed.ports.store import JobStore
from jobfeed.ports.store_views import StoreViewsMixin
from jobfeed.web.deps import get_store
from jobfeed.web.errors import ApiError

_HTTP_NOT_FOUND = 404
# A full first-time scan of every source has inserted about 700 postings; the
# bound keeps such scans whole while capping the payload for larger ones.
_NEW_JOBS_LIMIT = 1000

router = APIRouter()

_Store = Annotated[JobStore, Depends(get_store)]


class RunNewJobSourcesResponse(BaseModel):
    """Exact first-insert counts by user-configured scan source."""

    run_id: str
    source_counts: dict[str, int]
    total: int


@router.get("/runs/{run_id}/new-job-sources")
async def get_run_new_job_sources(
    run_id: str,
    store: _Store,
) -> RunNewJobSourcesResponse:
    """Return configured-source attribution for a scan's new jobs.

    Args:
        run_id: Historical or active pipeline run identity.
        store: Shared job store.

    Returns:
        Exact first-insert counts by configured source and their total.

    Raises:
        ApiError: 404 when the run is unknown.
    """
    run = await store.get_pipeline_run(run_id)
    if run is None:
        raise ApiError(_HTTP_NOT_FOUND, "not_found", f"run {run_id} not found")
    counts = await cast(StoreViewsMixin, store).get_new_job_source_counts(run_id)
    return RunNewJobSourcesResponse(
        run_id=run_id,
        source_counts=counts,
        total=sum(counts.values()),
    )


class RunNewJobOut(BaseModel):
    """One posting a scan inserted for the first time."""

    job_id: str
    title: str
    company: str
    location: str
    source: str
    platform: str
    url: str
    first_seen_at: datetime


class RunNewJobsResponse(BaseModel):
    """A scan's first-inserted postings, oldest first, bounded."""

    run_id: str
    jobs: list[RunNewJobOut]
    total: int
    is_truncated: bool


@router.get("/runs/{run_id}/new-jobs")
async def get_run_new_jobs(run_id: str, store: _Store) -> RunNewJobsResponse:
    """Return the postings a scan inserted for the first time.

    Args:
        run_id: Historical or active pipeline run identity.
        store: Shared job store.

    Returns:
        Up to ``_NEW_JOBS_LIMIT`` postings ordered by first-seen time, the
        unbounded total, and whether the bound left postings out. Evaluation
        runs return an empty list.

    Raises:
        ApiError: 404 when the run is unknown.
    """
    run = await store.get_pipeline_run(run_id)
    if run is None:
        raise ApiError(_HTTP_NOT_FOUND, "not_found", f"run {run_id} not found")
    page = await cast(StoreViewsMixin, store).get_new_jobs(
        run_id, limit=_NEW_JOBS_LIMIT
    )
    return RunNewJobsResponse(
        run_id=run_id,
        jobs=[
            RunNewJobOut(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                source=job.source,
                platform=job.platform,
                url=job.url,
                first_seen_at=job.first_seen_at,
            )
            for job in page.jobs
        ],
        total=page.total,
        is_truncated=page.is_truncated,
    )
