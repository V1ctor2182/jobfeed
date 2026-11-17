"""Read the recruiter sheet and save the user's edits. Nothing here sends anything."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from jobfeed.config import Settings, load_settings
from jobfeed.domain.recruiter_sheet import (
    TRANSITIONS,
    SheetEdit,
    SheetPolicy,
    SheetRow,
    SheetStatus,
)
from jobfeed.recruiter_sheet import (
    EditResult,
    apply_edits,
    read_sheet,
    render_csv,
    timestamp,
)
from jobfeed.web.deps import get_context
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.my_profile import _mutation

router = APIRouter()
_MAX_EDITS = 500
_MAX_FIELD = 20_000
_UNAVAILABLE = (
    "The recruiter sheet could not be read. Check config.toml and the database."
)


class RecruiterSheetRowResponse(BaseModel):
    """One sheet row. ``real_job_id`` is the stable key a sheet column can carry."""

    real_job_id: int
    source_job_ids: list[int]
    company: str
    title: str
    location: str
    fit: int | None
    verdict: str | None
    why: str
    watch_out: str
    url: str | None
    status: SheetStatus
    recruiter: str
    note: str
    updated_at: str | None
    status_updated_at: str | None
    closed: bool
    applied: bool
    queue_state: str | None
    qualifies: bool
    pay: int | None
    funding: str
    funding_source: str | None
    funding_pending: bool


class RecruiterSheetResponse(BaseModel):
    """Every row, best fit first."""

    as_of: str
    rows: list[RecruiterSheetRowResponse]


class RecruiterSheetRowStatus(BaseModel):
    """A row's status and when the user last changed it. Null means never."""

    real_job_id: int
    status: SheetStatus
    status_updated_at: str | None


class RecruiterSheetStatusesResponse(BaseModel):
    """The status table, and each row's status for telling which rows changed."""

    as_of: str
    statuses: list[SheetStatus]
    transitions: dict[SheetStatus, list[SheetStatus]]
    rows: list[RecruiterSheetRowStatus]


class RecruiterSheetEditBody(BaseModel):
    """One row's new values. An unknown status is refused for its row alone.

    An omitted recruiter or note keeps the row's current value.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(max_length=_MAX_FIELD)
    recruiter: str | None = Field(default=None, max_length=_MAX_FIELD)
    note: str | None = Field(default=None, max_length=_MAX_FIELD)


class RecruiterSheetKeyedEdit(RecruiterSheetEditBody):
    """One edit in a batch, keyed by real job ID."""

    real_job_id: int = Field(gt=0, lt=2**63)


class RecruiterSheetEditsBody(BaseModel):
    """A batch of edits, applied in order."""

    model_config = ConfigDict(extra="forbid")

    edits: list[RecruiterSheetKeyedEdit] = Field(max_length=_MAX_EDITS)


class RecruiterSheetEditResult(BaseModel):
    """What became of one edit in a batch."""

    real_job_id: int
    outcome: Literal["applied", "unchanged", "rejected"]
    code: str | None
    message: str | None
    row: RecruiterSheetRowResponse | None


class RecruiterSheetEditsResponse(BaseModel):
    """One result per edit, in request order."""

    results: list[RecruiterSheetEditResult]


def _source(request: Request) -> tuple[Path, SheetPolicy]:
    """Read the rules from the file each time, as the autopilot hold does."""
    context = get_context(request)
    runtime: Settings = context["settings"]
    path = context.get("config_path")
    settings = load_settings(path) if path else runtime
    return runtime.db.path, settings.recruiter_sheet.to_domain()


def _rows(request: Request) -> list[SheetRow]:
    try:
        db_path, policy = _source(request)
        return read_sheet(db_path, policy)
    except (OSError, ValueError, sqlite3.Error):
        raise ApiError(503, "recruiter_sheet_unavailable", _UNAVAILABLE) from None


def _edit(request: Request, edits: list[SheetEdit]) -> list[EditResult]:
    try:
        db_path, policy = _source(request)
        return apply_edits(db_path, policy, edits)
    except (OSError, ValueError, sqlite3.Error):
        raise ApiError(503, "recruiter_sheet_unavailable", _UNAVAILABLE) from None


def _row_response(row: SheetRow) -> RecruiterSheetRowResponse:
    return RecruiterSheetRowResponse(
        real_job_id=row.real_job_id,
        source_job_ids=list(row.source_job_ids),
        company=row.company,
        title=row.title,
        location=row.location,
        fit=row.fit,
        verdict=row.verdict,
        why=row.why,
        watch_out=row.watch_out,
        url=row.url,
        status=row.status,
        recruiter=row.recruiter,
        note=row.note,
        updated_at=row.updated_at,
        status_updated_at=row.status_updated_at,
        closed=row.closed,
        applied=row.applied,
        queue_state=row.queue_state,
        qualifies=row.qualifies,
        pay=row.pay,
        funding=row.funding,
        funding_source=row.funding_source,
        funding_pending=row.funding_pending,
    )


@router.get("/recruiter-sheet", response_model=RecruiterSheetResponse)
def get_recruiter_sheet(request: Request, response: Response) -> RecruiterSheetResponse:
    """Return every row as stored, with posting text unescaped.

    A client that writes these cells into a spreadsheet must neutralize formulas or use
    a raw input mode; only the CSV endpoint neutralizes them.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The rows and the time they were read.

    Raises:
        ApiError: If the sheet cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    as_of = timestamp()
    return RecruiterSheetResponse(
        as_of=as_of, rows=[_row_response(row) for row in _rows(request)]
    )


@router.get("/recruiter-sheet/statuses", response_model=RecruiterSheetStatusesResponse)
def get_recruiter_sheet_statuses(
    request: Request, response: Response
) -> RecruiterSheetStatusesResponse:
    """Return the allowed statuses and each row's status with its timestamp.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The status table and one entry per row.

    Raises:
        ApiError: If the sheet cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    as_of = timestamp()
    return RecruiterSheetStatusesResponse(
        as_of=as_of,
        statuses=list(SheetStatus),
        transitions={
            status: sorted(targets) for status, targets in TRANSITIONS.items()
        },
        rows=[
            RecruiterSheetRowStatus(
                real_job_id=row.real_job_id,
                status=row.status,
                status_updated_at=row.status_updated_at,
            )
            for row in _rows(request)
        ],
    )


@router.get("/recruiter-sheet.csv", response_class=Response)
def get_recruiter_sheet_csv(request: Request) -> Response:
    """Return the sheet as CSV with spreadsheet formulas neutralized.

    Args:
        request: Incoming request carrying the app state.

    Returns:
        A CSV attachment.

    Raises:
        ApiError: If the sheet cannot be read.
    """
    return Response(
        render_csv(_rows(request)),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="recruiter-sheet.csv"',
        },
    )


@router.post("/recruiter-sheet/edits", response_model=RecruiterSheetEditsResponse)
def post_recruiter_sheet_edits(
    body: RecruiterSheetEditsBody, request: Request, response: Response
) -> RecruiterSheetEditsResponse:
    """Apply a batch of edits. A bad edit is rejected alone; the rest are saved.

    Args:
        body: The edits, each keyed by real job ID.
        request: Incoming request, checked as a dashboard mutation.
        response: Response whose cache headers are set.

    Returns:
        One result per edit. A repeated edit reports ``unchanged``.

    Raises:
        ApiError: If the origin is refused or the sheet cannot be read or written.
    """
    _mutation(request)
    response.headers["Cache-Control"] = "no-store"
    results = _edit(
        request,
        [
            SheetEdit(edit.real_job_id, edit.status, edit.recruiter, edit.note)
            for edit in body.edits
        ],
    )
    return RecruiterSheetEditsResponse(
        results=[
            RecruiterSheetEditResult(
                real_job_id=result.real_job_id,
                outcome=result.outcome,  # type: ignore[arg-type]
                code=result.problem.code if result.problem else None,
                message=result.problem.message if result.problem else None,
                row=_row_response(result.row) if result.row else None,
            )
            for result in results
        ]
    )


@router.put("/recruiter-sheet/{real_job_id}", response_model=RecruiterSheetRowResponse)
def put_recruiter_sheet_row(
    real_job_id: Annotated[int, Field(gt=0, lt=2**63)],
    body: RecruiterSheetEditBody,
    request: Request,
    response: Response,
) -> RecruiterSheetRowResponse:
    """Save one row's status, recruiter and note.

    Args:
        real_job_id: The row's stable key.
        body: The new values.
        request: Incoming request, checked as a dashboard mutation.
        response: Response whose cache headers are set.

    Returns:
        The saved row.

    Raises:
        ApiError: If the origin is refused, the job is not on the sheet, the edit is
            refused, or the sheet cannot be read or written.
    """
    _mutation(request)
    response.headers["Cache-Control"] = "no-store"
    [result] = _edit(
        request, [SheetEdit(real_job_id, body.status, body.recruiter, body.note)]
    )
    if result.problem is not None:
        status = 404 if result.problem.code == "not_on_sheet" else 409
        raise ApiError(status, result.problem.code, result.problem.message)
    assert result.row is not None
    return _row_response(result.row)
