"""Read saved Apply observations without invoking application workers."""

from __future__ import annotations

import sqlite3
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response

from jobfeed.application_forms import read_application_form, read_form_resume
from jobfeed.apply_health import ApplyHealthReader, EvidenceError
from jobfeed.services.application_form_models import ApplicationFormResponse
from jobfeed.services.apply_health_models import (
    ApplyHealthResponse,
    ConfirmationResponse,
)
from jobfeed.web.deps import get_context

router = APIRouter()


@router.get("/apply-health/jobs/{job_id}/form", response_model=ApplicationFormResponse)
def get_application_form(
    job_id: int, batch_id: str, request: Request, response: Response
) -> ApplicationFormResponse:
    """Return the saved form observations for one job in a batch.

    Args:
        job_id: Job whose form should be read.
        batch_id: Batch the application run belongs to.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The saved snapshots, or why none are available.
    """
    reader = cast(ApplyHealthReader, request.app.state.apply_health_reader)
    response.headers["Cache-Control"] = "no-store"
    return read_application_form(
        reader.root, get_context(request)["settings"].db.path, job_id, batch_id
    )


@router.get("/apply-health/jobs/{job_id}/form/resume")
def get_form_resume(
    job_id: int, batch_id: str, snapshot_id: str, request: Request
) -> Response:
    """Return the résumé PDF attached to one saved form snapshot.

    Args:
        job_id: Job whose form should be read.
        batch_id: Batch the application run belongs to.
        snapshot_id: Snapshot whose résumé should be returned.
        request: Incoming request carrying the app state.

    Returns:
        The PDF as an uncached response.

    Raises:
        HTTPException: 404 if the résumé cannot be read.
    """
    reader = cast(ApplyHealthReader, request.app.state.apply_health_reader)
    try:
        data = read_form_resume(
            reader.root,
            get_context(request)["settings"].db.path,
            job_id,
            batch_id,
            snapshot_id,
        )
    except (ValueError, OSError, RuntimeError, sqlite3.Error, EvidenceError, TypeError):
        raise HTTPException(
            status_code=404, detail="Saved resume unavailable"
        ) from None
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": (
                f'inline; filename="application-{job_id}-resume.pdf"'
            ),
        },
    )


@router.get("/apply-health", response_model=ApplyHealthResponse)
def get_apply_health(request: Request) -> ApplyHealthResponse:
    """Return saved application observations without running a diagnostic.

    Args:
        request: Request bound to the configured dashboard dependency graph.

    Returns:
        Allowlisted queue and monitor observations from the configured runtime.
    """
    reader = cast(ApplyHealthReader, request.app.state.apply_health_reader)
    return reader.read(get_context(request)["settings"].db.path)


@router.get(
    "/apply-health/jobs/{job_id}/confirmation", response_model=ConfirmationResponse
)
def get_confirmation(job_id: int, request: Request) -> ConfirmationResponse:
    """Return a summary of a job's submission confirmation evidence.

    Args:
        job_id: Job whose confirmation should be read.
        request: Incoming request carrying the app state.

    Returns:
        The evidence summary, or an unverified status.
    """
    reader = cast(ApplyHealthReader, request.app.state.apply_health_reader)
    return reader.confirmation_response(
        get_context(request)["settings"].db.path, job_id
    )


@router.get("/apply-health/jobs/{job_id}/confirmation/artifact")
def get_confirmation_artifact(job_id: int, request: Request) -> Response:
    """Return the screenshot saved as a job's submission confirmation.

    Args:
        job_id: Job whose confirmation should be read.
        request: Incoming request carrying the app state.

    Returns:
        The screenshot image as an uncached response.

    Raises:
        HTTPException: 404 if the evidence is unavailable or is not a website
            screenshot.
    """
    reader = cast(ApplyHealthReader, request.app.state.apply_health_reader)
    try:
        evidence = reader.confirmation(get_context(request)["settings"].db.path, job_id)
    except (ValueError, OSError, RuntimeError, sqlite3.Error, EvidenceError):
        raise HTTPException(
            status_code=404, detail="Confirmation evidence unavailable"
        ) from None
    if evidence.kind != "website_screenshot":
        raise HTTPException(status_code=404, detail="Screenshot unavailable")
    extension = "jpg" if evidence.media_type == "image/jpeg" else "png"
    return Response(
        content=evidence.artifact_bytes,
        media_type=evidence.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": (
                f'inline; filename="confirmation-{job_id}.{extension}"'
            ),
        },
    )
