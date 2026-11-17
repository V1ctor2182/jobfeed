"""Open Applier questions and the user's own replies to them from the dashboard."""

from typing import cast

from fastapi import APIRouter, Request, Response

from jobfeed.review import (
    ReviewError,
    ReviewIgnore,
    ReviewReply,
    ReviewResume,
    ReviewService,
    ReviewState,
)
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError

router = APIRouter()


def _service(request: Request) -> ReviewService:
    return cast(ReviewService, request.app.state.review_service)


def _error(exc: ReviewError) -> ApiError:
    status = {"invalid": 422, "unavailable": 503}[exc.code]
    return ApiError(status, f"review_{exc.code}", str(exc))


def _require_dashboard(request: Request) -> None:
    expected = dashboard_loopback_origin(
        request, browser_origin=request.headers.get("origin", "")
    )
    if (
        expected is None
        or len(request.headers.getlist("origin")) != 1
        or len(request.headers.getlist("host")) != 1
    ):
        raise ApiError(403, "review_origin", "Answer questions from this dashboard.")


@router.get("/review", response_model=ReviewState)
def get_review(request: Request, response: Response) -> ReviewState:
    """Return every open question and blocker.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The open requests, oldest first.

    Raises:
        ApiError: If the open requests cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).state()
    except ReviewError as exc:
        raise _error(exc) from None


@router.post("/review/reply", response_model=ReviewState)
def post_reply(body: ReviewReply, request: Request, response: Response) -> ReviewState:
    """Save the user's reply to one open request.

    Args:
        body: The request, the kind of reply and its text.
        request: Incoming request; it must come from the dashboard's own origin.
        response: Response whose cache headers are set.

    Returns:
        The open requests after the reply.

    Raises:
        ApiError: If the request is not from the dashboard or the reply is rejected.
    """
    _require_dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).reply(body)
    except ReviewError as exc:
        raise _error(exc) from None


@router.post("/review/ignore", response_model=ReviewState)
def post_ignore(
    body: ReviewIgnore, request: Request, response: Response
) -> ReviewState:
    """Record the user's decision to stop one job.

    Args:
        body: The job to stop.
        request: Incoming request; it must come from the dashboard's own origin.
        response: Response whose cache headers are set.

    Returns:
        The open requests after the job was stopped.

    Raises:
        ApiError: If the request is not from the dashboard or the job cannot be stopped.
    """
    _require_dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).ignore(body)
    except ReviewError as exc:
        raise _error(exc) from None


@router.post("/review/resume-site", response_model=ReviewState)
def post_resume_site(
    body: ReviewResume, request: Request, response: Response
) -> ReviewState:
    """Resume a job board after the user signed in there.

    Args:
        body: The job board to resume.
        request: Incoming request; it must come from the dashboard's own origin.
        response: Response whose cache headers are set.

    Returns:
        The open requests after the board was resumed.

    Raises:
        ApiError: If the request is not from the dashboard or the board is not held.
    """
    _require_dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).resume_site(body)
    except ReviewError as exc:
        raise _error(exc) from None
