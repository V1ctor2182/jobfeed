"""Application history and guarded legacy mark-applied routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from jobfeed.ports.store import JobStore
from jobfeed.services.application import ApplicationService
from jobfeed.web.deps import get_application_service, get_store
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.real_jobs_workflow import RealJobWorkflowPort
from jobfeed.web.schemas import (
    ApplicationsListResponse,
    ApplyResponse,
    applications_list_response,
)
from jobfeed.web.schemas.applications import (
    RealJobApplicationsListResponse,
    real_job_applications_response,
)

_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_HTTP_VALIDATION_ERROR = 422
_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 1000

router = APIRouter()

_Applications = Annotated[ApplicationService, Depends(get_application_service)]
_Store = Annotated[JobStore, Depends(get_store)]


@dataclass(kw_only=True, frozen=True)
class ApplyForm:
    """Parsed multipart fields of the apply request."""

    tailored: UploadFile | None
    cover_letter: UploadFile | None
    variant: str | None
    method: str | None
    notes: str | None
    source_job_id: int | None
    apply_url: str | None


async def _apply_form(  # noqa: PLR0913
    tailored: Annotated[UploadFile | None, File()] = None,
    cover_letter: Annotated[UploadFile | None, File()] = None,
    variant: Annotated[str | None, Form()] = None,
    method: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    source_job_id: Annotated[int | None, Form()] = None,
    apply_url: Annotated[str | None, Form()] = None,
) -> ApplyForm:
    """Collect the optional multipart upload + form fields (plan D8).

    Args:
        tailored: Optional tailored resume file.
        cover_letter: Optional cover letter file.
        variant: Optional resume variant name.
        method: Optional application method.
        notes: Optional free-form note.

    Returns:
        Parsed form bundle.
    """
    return ApplyForm(
        tailored=tailored,
        cover_letter=cover_letter,
        variant=variant,
        method=method,
        notes=notes,
        source_job_id=source_job_id,
        apply_url=apply_url,
    )


@router.post("/jobs/{job_id}/apply")
async def apply_to_job(
    job_id: int,
    service: _Applications,
    store: _Store,
) -> ApplyResponse:
    """Refuse unverified new submissions while preserving existing history.

    Args:
        job_id: Source job ID to record as applied.
        service: Application service that records the outcome.
        store: Store used to find the job and its real job identity.

    Returns:
        The apply outcome for the job.

    Raises:
        ApiError: If the job does not exist or its real job identity cannot be
            resolved.
    """
    if await store.get_job(str(job_id)) is None:
        raise ApiError(_HTTP_NOT_FOUND, "not_found", f"job {job_id} not found")
    try:
        await cast(RealJobWorkflowPort, store).resolve_real_job_id(str(job_id))
    except ValueError as exc:
        raise ApiError(_HTTP_CONFLICT, "identity_unresolved", str(exc)) from exc
    return await _record_apply(
        source_job_id=job_id,
        service=service,
    )


@router.post("/real-jobs/{real_job_id}/apply")
async def apply_to_real_job(
    real_job_id: int,
    form: Annotated[ApplyForm, Depends(_apply_form)],
    service: _Applications,
    store: _Store,
) -> ApplyResponse:
    """Guard canonical submission writes while retaining existing history.

    Args:
        real_job_id: Canonical job receiving the application.
        form: Submitted source selection, resume content, and application metadata.
        service: Injected application or job-view service.
        store: Store providing the state or posting reads required by this operation.

    Returns:
        Application result with the canonical job identity.

    Raises:
        ApiError: If the source is missing, invalid, or inconsistent with the canonical
            job.
    """
    if form.source_job_id is None:
        raise ApiError(
            _HTTP_VALIDATION_ERROR,
            "validation_error",
            "source_job_id is required",
        )
    if await store.get_job(str(form.source_job_id)) is None:
        raise ApiError(
            _HTTP_NOT_FOUND, "not_found", f"job {form.source_job_id} not found"
        )
    try:
        resolved = await cast(RealJobWorkflowPort, store).resolve_real_job_id(
            str(form.source_job_id)
        )
    except ValueError as exc:
        raise ApiError(_HTTP_CONFLICT, "identity_unresolved", str(exc)) from exc
    if resolved != str(real_job_id):
        raise ApiError(_HTTP_CONFLICT, "identity_mismatch", "Source does not match job")
    return await _record_apply(
        source_job_id=form.source_job_id,
        service=service,
    )


async def _record_apply(
    *,
    source_job_id: int,
    service: ApplicationService,
) -> ApplyResponse:
    if await service.get_application(str(source_job_id)) is not None:
        return ApplyResponse(applied=False, reapply_notice=None)
    raise ApiError(
        _HTTP_CONFLICT,
        "confirmation_required",
        "A saved confirmation screenshot or email is required. "
        "Record the submission through the application pipeline "
        "after evidence is saved.",
    )


@router.get("/applications")
async def list_applications(
    service: _Applications,
    limit: Annotated[int, Query(ge=1, le=_MAX_HISTORY_LIMIT)] = _DEFAULT_HISTORY_LIMIT,
) -> ApplicationsListResponse:
    """List recent application records, newest first.

    Args:
        service: Shared application service from the app state.
        limit: Maximum number of records to return.

    Returns:
        Application history rows (audit metadata + snapshot hashes only).
    """
    records = await service.apply_history(limit=limit)
    return applications_list_response(records)


@router.get("/real-jobs/applications")
async def list_real_job_applications(
    service: _Applications,
    limit: Annotated[int, Query(ge=1, le=_MAX_HISTORY_LIMIT)] = _DEFAULT_HISTORY_LIMIT,
) -> RealJobApplicationsListResponse:
    """List canonical submission events, including their source provenance.

    Args:
        service: Injected application or job-view service.
        limit: Maximum number of records to select.

    Returns:
        Serialized canonical application history.
    """
    return real_job_applications_response(
        await service.real_job_apply_history(limit=limit)
    )


__all__ = ["router"]
