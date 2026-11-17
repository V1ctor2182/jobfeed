"""Read-only view of application email and the stage each application is in."""

from typing import cast

from fastapi import APIRouter, Path, Request, Response

from jobfeed.tracker import (
    TrackerBody,
    TrackerError,
    TrackerNotFound,
    TrackerService,
    TrackerState,
)
from jobfeed.web.errors import ApiError

router = APIRouter()


@router.get("/tracker", response_model=TrackerState)
def get_tracker(request: Request, response: Response) -> TrackerState:
    """Return the application inbox and every application's stage.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        Saved application email and the applications it belongs to.

    Raises:
        ApiError: If the tracker cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return cast(TrackerService, request.app.state.tracker_service).state()
    except TrackerError as exc:
        raise ApiError(503, "tracker_unavailable", str(exc)) from None


@router.get("/tracker/messages/{message_id}", response_model=TrackerBody)
def get_message(
    request: Request,
    response: Response,
    message_id: str = Path(pattern=r"^[0-9a-f]{8,32}$"),
) -> TrackerBody:
    """Return one saved email with its plain-text body.

    The first read of a message fetches its body from Gmail, which takes a few
    seconds. Later reads come from the saved copy.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.
        message_id: The saved message to open.

    Returns:
        The message and its body.

    Raises:
        ApiError: If the tracker never saved this message or cannot fetch it.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return cast(TrackerService, request.app.state.tracker_service).body(message_id)
    except TrackerNotFound as exc:
        raise ApiError(404, "tracker_message_not_found", str(exc)) from None
    except TrackerError as exc:
        raise ApiError(503, "tracker_unavailable", str(exc)) from None
