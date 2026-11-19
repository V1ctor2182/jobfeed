"""Local dashboard admission of explicitly authorized draft preparation."""

import secrets
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message

from jobfeed.application_queue import ApplicationQueueError, ApplicationQueueService
from jobfeed.services.application_queue_models import (
    ApplicationHandoffRequest,
    ApplicationHandoffState,
    ApplicationQueueReceipt,
    ApplicationQueueRequest,
    ApplicationQueueState,
)
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError

_REQUEST_LIMIT = 65536


class BoundedQueueRoute(APIRoute):
    """API route that rejects request bodies larger than the queue request limit."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the route handler so request bodies are counted while they stream in.

        Returns:
            A handler that raises HTTP 413 once the body exceeds the request limit.
        """
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            received = 0

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > _REQUEST_LIMIT:
                    raise HTTPException(413, "Application queue request is too large.")
                return message

            return await handler(Request(request.scope, receive))

        return bounded


router = APIRouter(route_class=BoundedQueueRoute)


def _dashboard(request: Request, *, mutation: bool = False) -> str:
    origin = dashboard_loopback_origin(
        request, browser_origin=request.headers.get("origin", "") if mutation else None
    )
    if origin is None or len(request.headers.getlist("host")) != 1:
        raise ApiError(403, "application_queue_origin", "Use the local dashboard.")
    if mutation and (
        len(request.headers.getlist("origin")) != 1
        or len(request.headers.getlist("x-application-queue-csrf")) != 1
        or not secrets.compare_digest(
            request.headers.get("x-application-queue-csrf", ""),
            request.app.state.application_queue.csrf_token,
        )
    ):
        raise ApiError(
            403,
            "application_queue_csrf",
            "Use this dashboard's queue or handoff action.",
        )
    return request.headers["origin"] if mutation else origin


def _error(exc: ApplicationQueueError) -> ApiError:
    return ApiError(
        {"invalid": 422, "conflict": 409, "unavailable": 503}[exc.code],
        "application_queue_" + exc.code,
        str(exc),
    )


@router.get("/application-queue", response_model=ApplicationQueueState)
async def state(
    request: Request, response: Response, request_id: UUID | None = None
) -> ApplicationQueueState:
    """Return the preparation queue status.

    Args:
        request: Incoming dashboard request.
        response: Response whose cache headers are set.
        request_id: Limit the status to this request, if given.

    Returns:
        Queue requests, consumer status and the CSRF token.

    Raises:
        ApiError: If the queue status is unavailable.
    """
    _dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    service: ApplicationQueueService = request.app.state.application_queue
    try:
        return await service.state(str(request_id) if request_id else None)
    except ApplicationQueueError as exc:
        raise _error(exc) from None


@router.get("/application-queue/handoff", response_model=ApplicationHandoffState)
async def handoff_state(
    request: Request, response: Response, request_id: UUID
) -> ApplicationHandoffState:
    """Return the status of one handoff request.

    Args:
        request: Incoming dashboard request.
        response: Response whose cache headers are set.
        request_id: ID of the handoff request.

    Returns:
        The handoff's receipt, if the request exists.

    Raises:
        ApiError: If the request is not a handoff or the queue is unavailable.
    """
    _dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    service: ApplicationQueueService = request.app.state.application_queue
    try:
        return await service.handoff_state(str(request_id))
    except ApplicationQueueError as exc:
        raise _error(exc) from None


@router.post(
    "/application-queue/handoff",
    response_model=ApplicationQueueReceipt,
    status_code=202,
)
async def handoff(
    body: ApplicationHandoffRequest, request: Request, response: Response
) -> ApplicationQueueReceipt:
    """Hand selected jobs to an Applier runtime from the local dashboard.

    Args:
        body: Request ID, job selection, runtime and explicit authorization.
        request: Incoming dashboard mutation request.
        response: Response whose cache headers are set.

    Returns:
        The receipt for the handoff request.

    Raises:
        ApiError: If the request does not come from the loopback dashboard or
            the queue rejects it.
    """
    origin = _dashboard(request, mutation=True)
    backend_origin = dashboard_loopback_origin(request, browser_origin=origin)
    if backend_origin is None:
        raise ApiError(403, "application_queue_origin", "Use the local dashboard.")
    response.headers["Cache-Control"] = "no-store"
    service: ApplicationQueueService = request.app.state.application_queue
    try:
        return await service.handoff(body, origin, backend_origin)
    except ApplicationQueueError as exc:
        raise _error(exc) from None


@router.post(
    "/application-queue", response_model=ApplicationQueueReceipt, status_code=202
)
async def enqueue(
    body: ApplicationQueueRequest, request: Request, response: Response
) -> ApplicationQueueReceipt:
    """Queue canonical real jobs for preparation.

    Args:
        body: Request ID and the real job IDs to queue.
        request: Incoming dashboard mutation request.
        response: Response whose cache headers are set.

    Returns:
        The receipt for the queue request.

    Raises:
        ApiError: If the queue rejects the request.
    """
    origin = _dashboard(request, mutation=True)
    response.headers["Cache-Control"] = "no-store"
    service: ApplicationQueueService = request.app.state.application_queue
    try:
        return await service.request(body, origin)
    except ApplicationQueueError as exc:
        raise _error(exc) from None
