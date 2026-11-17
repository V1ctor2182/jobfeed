"""Explicit local-dashboard commands for existing application agents."""

import secrets
from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request, Response

from jobfeed.application_reruns import ApplicationRerunError, ApplicationRerunService
from jobfeed.services.application_rerun_models import (
    ApplicationRerunRequest,
    ApplicationRerunState,
)
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.application_tabs import (
    BoundedTabRoute,
    _allowed_origin,
    profile_availability,
)

router = APIRouter(route_class=BoundedTabRoute)
_MAX_PORT = 65535


def _service(request: Request) -> ApplicationRerunService:
    return cast(ApplicationRerunService, request.app.state.application_reruns)


def _dashboard(request: Request, *, mutation: bool = False) -> None:
    host = request.headers.get("host", "")
    if not _allowed_origin("http://" + host):
        raise ApiError(403, "application_rerun_origin", "Use the local dashboard.")
    if mutation:
        origin = request.headers.get("origin", "")
        if (
            not _allowed_origin(origin)
            or urlsplit(origin).netloc != host
            or not secrets.compare_digest(
                request.headers.get("x-application-reruns-csrf", ""),
                _service(request).csrf_token,
            )
        ):
            raise ApiError(
                403,
                "application_rerun_csrf",
                "Use this dashboard's Rerun application button.",
            )


def _error(exc: ApplicationRerunError) -> ApiError:
    return ApiError(
        {"invalid": 422, "conflict": 409, "unavailable": 503}[exc.code],
        "application_rerun_" + exc.code,
        str(exc),
    )


@router.get("/application-reruns", response_model=ApplicationRerunState)
def state(
    request: Request,
    response: Response,
    batch_id: Annotated[str, Query(min_length=1, max_length=200)],
    job_id: Annotated[int, Query(gt=0)],
) -> ApplicationRerunState:
    """Return whether a job can be rerun, and its latest rerun request.

    A job is not eligible while the extension cannot probe Chrome profiles.

    Args:
        request: Incoming dashboard request.
        response: Response whose cache headers are set.
        batch_id: Batch the job belongs to.
        job_id: Job to check.

    Returns:
        The rerun state.

    Raises:
        ApiError: If the rerun state is unavailable.
    """
    _dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        result = _service(request).state(batch_id, job_id)
        available, reason = profile_availability(request)
        if result.eligible and not available:
            return result.model_copy(update={"eligible": False, "reason": reason})
        return result
    except ApplicationRerunError as exc:
        raise _error(exc) from None


@router.post(
    "/application-reruns", response_model=ApplicationRerunState, status_code=202
)
def rerun(
    body: ApplicationRerunRequest, request: Request, response: Response
) -> ApplicationRerunState:
    """Request a rerun of one job's application from the local dashboard.

    Args:
        body: The rerun request.
        request: Incoming dashboard mutation request.
        response: Response whose cache headers are set.

    Returns:
        The rerun state after the request.

    Raises:
        ApplicationRerunError: Internally, when the extension cannot probe
            profiles or the server is not on a loopback address.
        ApiError: If the rerun cannot be requested.
    """
    _dashboard(request, mutation=True)
    response.headers["Cache-Control"] = "no-store"
    try:
        available, reason = profile_availability(request)
        if not available:
            raise ApplicationRerunError(
                "unavailable",
                reason or "Reload the Jobfeed extension before restarting.",
            )
        server = request.scope.get("server")
        if (
            not server
            or server[0] not in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
            or not 0 < server[1] <= _MAX_PORT
        ):
            raise ApplicationRerunError(
                "unavailable", "The local dashboard address could not be verified."
            )
        host = "[::1]" if server[0] in {"::1", "::"} else "127.0.0.1"
        return _service(request).request(body, f"http://{host}:{server[1]}")
    except ApplicationRerunError as exc:
        raise _error(exc) from None
