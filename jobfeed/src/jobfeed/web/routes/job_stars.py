"""Bounded star annotations without application admission or authorization."""

from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from jobfeed.domain.job_stars import JobStar
from jobfeed.web.deps import get_store
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.my_profile import _mutation

router = APIRouter()
_MAX_IDS = 100


class JobStarsPort(Protocol):
    """Store operations the star routes need."""

    async def get_job_stars(self, source_ids: list[str]) -> list[JobStar]:
        """Read stars for the given source job IDs.

        Args:
            source_ids: Source job IDs to look up.

        Returns:
            Stars for the known source jobs.
        """

    async def set_job_star(
        self, source_id: str, starred: bool, *, user_set: bool = True
    ) -> JobStar:
        """Set or clear the star on one source job.

        Args:
            source_id: Source job ID to annotate.
            starred: Whether the job should be starred.
            user_set: Whether the user set the star explicitly.

        Returns:
            The job's updated star.
        """


class StarUpdate(BaseModel):
    """Request body that sets or clears a job's star."""

    model_config = ConfigDict(extra="forbid", strict=True)
    starred: bool


class StarResponse(BaseModel):
    """A source job's star and its linked real job, if any."""

    source_job_id: str
    real_job_id: str | None
    starred: bool


class StarsResponse(BaseModel):
    """Stars for the requested source jobs."""

    stars: list[StarResponse]


def _port(store: Annotated[object, Depends(get_store)]) -> JobStarsPort:
    return cast(JobStarsPort, store)


_Store = Annotated[JobStarsPort, Depends(_port)]


def _response(value: JobStar) -> StarResponse:
    return StarResponse(
        source_job_id=value.source_job_id,
        real_job_id=value.real_job_id,
        starred=value.starred,
    )


@router.get("/job-stars", response_model=StarsResponse)
async def get_stars(
    job_ids: Annotated[str, Query(min_length=1, max_length=2100)],
    response: Response,
    store: _Store,
) -> StarsResponse:
    """Return known source IDs in request order; absent sources are omitted.

    Args:
        job_ids: Comma-separated positive source job IDs, at most 100.
        response: Response whose cache headers are set.
        store: Store that holds the stars.

    Returns:
        Stars for the known source jobs, without duplicates.

    Raises:
        ApiError: If there are too many IDs or one is not a positive integer.
    """
    values = job_ids.split(",")
    if len(values) > _MAX_IDS or any(
        not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**63
        for value in values
    ):
        raise ApiError(
            422, "invalid_job_ids", "Supply 1 to 100 positive source job IDs."
        )
    ids = list(dict.fromkeys(str(int(value)) for value in values))
    response.headers["Cache-Control"] = "no-store"
    return StarsResponse(
        stars=[_response(value) for value in await store.get_job_stars(ids)]
    )


@router.put("/jobs/{source_id}/star", response_model=StarResponse)
async def set_star(
    source_id: Annotated[int, Field(gt=0, lt=2**63)],
    body: StarUpdate,
    request: Request,
    response: Response,
    store: _Store,
) -> StarResponse:
    """Change an explicit annotation, preserving every application queue field.

    Args:
        source_id: Source job ID to annotate.
        body: Whether the job should be starred.
        request: Incoming request, checked as a dashboard mutation.
        response: Response whose cache headers are set.
        store: Store that holds the stars.

    Returns:
        The job's updated star.

    Raises:
        ApiError: If the source job does not exist.
    """
    _mutation(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _response(await store.set_job_star(str(source_id), body.starred))
    except KeyError as exc:
        raise ApiError(404, "not_found", "Source job not found.") from exc
