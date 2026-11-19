"""Pair a user-selected existing Chrome tab and request focus through its socket."""

import asyncio
import json
import re
import secrets
from collections.abc import Callable, Coroutine
from typing import Any, cast
from urllib.parse import urlsplit

from anyio import CancelScope
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.types import Message

from jobfeed.application_tabs import ApplicationTabError, ApplicationTabs, TabConnection
from jobfeed.services.application_tab_models import (
    ApplicationProfileProbeRequest,
    ApplicationProfileProof,
    ApplicationProfileValidateRequest,
    ApplicationProfileValidation,
    ApplicationTabAutoConnectRequest,
    ApplicationTabBinding,
    ApplicationTabConnectRequest,
    ApplicationTabCredentials,
    ApplicationTabFocusRequest,
    ApplicationTabFocusResult,
    ApplicationTabHello,
    ApplicationTabPairing,
    ApplicationTabPairingRequest,
    ApplicationTabReply,
    ApplicationTabsState,
)
from jobfeed.services.jobright_bridge import JobrightBridge
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError

_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
_PORTLESS_PORT = 1355
_MESSAGE_LIMIT = 8192


class BoundedTabRoute(APIRoute):
    """API route that rejects request bodies larger than the tab message limit."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the route handler so request bodies are counted while they stream in.

        Returns:
            A handler that raises HTTP 413 once the body exceeds the message limit.
        """
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            received = 0

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > _MESSAGE_LIMIT:
                    raise HTTPException(413, "Application tab request is too large.")
                return message

            return await handler(Request(request.scope, receive))

        return bounded


router = APIRouter(route_class=BoundedTabRoute)


async def _message(websocket: WebSocket) -> object:
    text = await websocket.receive_text()
    if len(text) > _MESSAGE_LIMIT:
        raise ApplicationTabError("tab_message_too_large", 413)
    return json.loads(text)


def _service(request: Request | WebSocket) -> ApplicationTabs:
    return cast(ApplicationTabs, request.app.state.application_tabs)


def _source_bridge(request: Request) -> JobrightBridge:
    context = getattr(request.app.state, "context", {})
    bridge = context.get("jobright_bridge")
    if not isinstance(bridge, JobrightBridge):
        raise ApplicationTabError("jobfeed_extension_unavailable", 503)
    return bridge


def profile_availability(request: Request) -> tuple[bool, str | None]:
    """Report whether the Jobfeed extension can probe Chrome profiles.

    Args:
        request: Incoming request carrying the app state.

    Returns:
        Whether probing is available, and the reason shown to the user when not.
    """
    try:
        bridge = _source_bridge(request)
    except ApplicationTabError:
        return False, "Jobfeed Chrome extension is unavailable."
    if not bridge.connected:
        return False, "Jobfeed Chrome extension is disconnected."
    if "application-tab-pair-v1" not in bridge.supported_sources:
        return False, "Reload the Jobfeed extension to connect application tabs."
    return True, None


def _error(exc: ApplicationTabError) -> ApiError:
    return ApiError(exc.status, f"application_tabs_{exc.code}", exc.message)


def _allowed_origin(origin: str) -> bool:
    if "\\" in origin or any(
        character.isspace() or not character.isprintable() for character in origin
    ):
        return False
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        return parsed.hostname in {"localhost", "127.0.0.1", "::1"} or (
            parsed.hostname == "jobfeed.localhost" and port == _PORTLESS_PORT
        )
    except ValueError:
        return False


def _dashboard(request: Request, *, csrf: bool = False) -> None:
    host = request.headers.get("host", "")
    if not _allowed_origin(f"http://{host}"):
        raise ApiError(403, "application_tabs_origin", "Use the local dashboard.")
    if csrf:
        origin = request.headers.get("origin", "")
        token = request.headers.get("x-application-tabs-csrf", "")
        same_origin = _allowed_origin(origin) and urlsplit(origin).netloc == host
        proxy_origin = dashboard_loopback_origin(request, browser_origin=origin)
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("host")) != 1
            or not (same_origin or proxy_origin is not None)
            or not secrets.compare_digest(token, _service(request).csrf_token)
        ):
            raise ApiError(
                403,
                "application_tabs_csrf",
                "Use this dashboard's application tab button.",
            )


def _extension(origin: str) -> None:
    if not _EXTENSION_ORIGIN.fullmatch(origin):
        raise ApplicationTabError("extension_origin_required", 403)


@router.get("/application-tabs", response_model=ApplicationTabsState)
async def application_tabs(
    request: Request, response: Response
) -> ApplicationTabsState:
    """Return the tab bindings, CSRF token and profile probe availability.

    Args:
        request: Incoming dashboard request.
        response: Response whose cache headers are set.

    Returns:
        The current application tab state.
    """
    _dashboard(request)
    response.headers["Cache-Control"] = "no-store"
    result = _service(request).state()
    result.profile_probe_available, result.profile_probe_reason = profile_availability(
        request
    )
    return result


@router.post("/application-tabs/profile-probe", response_model=ApplicationProfileProof)
async def profile_probe(
    body: ApplicationProfileProbeRequest, request: Request
) -> ApplicationProfileProof:
    """Ask the extension which candidate tabs are in its Chrome profile.

    Args:
        body: Candidate tabs to look for.
        request: Incoming dashboard request with a CSRF token.

    Returns:
        A short-lived proof of the matching tabs.

    Raises:
        ApiError: If the extension is unavailable or the probe fails.
    """
    _dashboard(request, csrf=True)
    try:
        return await _service(request).probe_profile(
            body.candidates, _source_bridge(request)
        )
    except ApplicationTabError as exc:
        raise _error(exc) from None


@router.post(
    "/application-tabs/profile-validate", response_model=ApplicationProfileValidation
)
async def profile_validate(
    body: ApplicationProfileValidateRequest, request: Request
) -> ApplicationProfileValidation:
    """Check that a profile proof is still valid for the current extension connection.

    Args:
        body: The profile proof to check.
        request: Incoming dashboard request with a CSRF token.

    Returns:
        The proof's validity and expiry.

    Raises:
        ApiError: If the proof expired or the extension reconnected.
    """
    _dashboard(request, csrf=True)
    try:
        proof = _service(request).profile(body.profile_proof, _source_bridge(request))
        return ApplicationProfileValidation(expires_at=proof.expires_at.isoformat())
    except ApplicationTabError as exc:
        raise _error(exc) from None


@router.post(
    "/application-tabs/{job_id}/auto-connect", response_model=ApplicationTabBinding
)
async def auto_connect(
    job_id: int, body: ApplicationTabAutoConnectRequest, request: Request
) -> ApplicationTabBinding:
    """Bind a proven, recorded tab to a job through the extension.

    Args:
        job_id: Job whose recorded tab should be bound.
        body: Owner thread, profile proof and the tab to connect.
        request: Incoming dashboard request with a CSRF token.

    Returns:
        The confirmed binding.

    Raises:
        ApiError: If the tab cannot be bound.
    """
    _dashboard(request, csrf=True)
    try:
        return await _service(request).auto_connect(
            job_id, body, _source_bridge(request), request.headers["origin"]
        )
    except ApplicationTabError as exc:
        raise _error(exc) from None


@router.post("/application-tabs/{job_id}/pairing", response_model=ApplicationTabPairing)
async def pairing(
    job_id: int,
    body: ApplicationTabPairingRequest,
    request: Request,
    response: Response,
) -> ApplicationTabPairing:
    """Issue a pairing code for a job's recorded tab.

    Args:
        job_id: Job whose recorded tab will be paired.
        body: Owner thread the tab was recorded for.
        request: Incoming dashboard request with a CSRF token.
        response: Response whose cache headers are set.

    Returns:
        The pairing code and its expiry.

    Raises:
        ApiError: If the job has no recorded tab or pairing capacity is full.
    """
    _dashboard(request, csrf=True)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).pairing(job_id, body.owner_thread_id)
    except ApplicationTabError as exc:
        raise _error(exc) from None


@router.options("/application-tabs/connect", include_in_schema=False)
def connect_preflight(request: Request) -> Response:
    """Answer the CORS preflight for the extension's connect request.

    Args:
        request: Preflight request from the extension.

    Returns:
        An empty response that allows POST from the extension origin.

    Raises:
        ApiError: If the origin is not the Jobfeed extension.
    """
    origin = request.headers.get("origin", "")
    try:
        _extension(origin)
    except ApplicationTabError as exc:
        raise _error(exc) from None
    return Response(
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }
    )


@router.post("/application-tabs/connect", response_model=ApplicationTabCredentials)
async def connect(
    body: ApplicationTabConnectRequest, request: Request, response: Response
) -> ApplicationTabCredentials:
    """Redeem a pairing code from the extension and issue tab credentials.

    Args:
        body: Pairing code, selected tab and browser session.
        request: Incoming request from the extension.
        response: Response whose CORS and cache headers are set.

    Returns:
        The binding ID and session token for the tab.

    Raises:
        ApiError: If the origin is not the extension or the pairing is rejected.
    """
    origin = request.headers.get("origin", "")
    try:
        _extension(origin)
        result = _service(request).connect(body, origin)
    except ApplicationTabError as exc:
        raise _error(exc) from None
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Origin"
    return result


@router.post(
    "/application-tabs/{job_id}/focus", response_model=ApplicationTabFocusResult
)
async def focus(
    job_id: int, body: ApplicationTabFocusRequest, request: Request
) -> ApplicationTabFocusResult:
    """Ask the extension to focus a bound tab.

    Args:
        job_id: Job the binding belongs to.
        body: Owner thread and binding to focus.
        request: Incoming dashboard request with a CSRF token.

    Returns:
        The focus status reported by the extension.

    Raises:
        ApiError: If the binding is unusable or the focus is not confirmed.
    """
    _dashboard(request, csrf=True)
    try:
        status = await _service(request).focus(
            job_id, body.owner_thread_id, body.binding_id
        )
        return ApplicationTabFocusResult(status=status)
    except ApplicationTabError as exc:
        raise _error(exc) from None


@router.post(
    "/application-tabs/{job_id}/open-existing", response_model=ApplicationTabFocusResult
)
async def open_existing(
    job_id: int, body: ApplicationTabPairingRequest, request: Request
) -> ApplicationTabFocusResult:
    """Focus a job's registered tab, reconnecting it when needed.

    Args:
        job_id: Job whose registered tab should be opened.
        body: Owner thread the tab was recorded for.
        request: Incoming dashboard request with a CSRF token.

    Returns:
        The focus status reported by the extension.

    Raises:
        ApiError: If the tab cannot be reconnected or focused.
    """
    _dashboard(request, csrf=True)
    try:
        status = await _service(request).open_existing(
            job_id,
            body.owner_thread_id,
            _source_bridge(request),
            request.headers["origin"],
        )
        return ApplicationTabFocusResult(status=status)
    except ApplicationTabError as exc:
        raise _error(exc) from None


async def _send(websocket: WebSocket, connection: TabConnection) -> None:
    while True:
        message = await connection.commands.get()
        if message["type"] == "revoked":
            await websocket.close(code=1008)
            return
        await websocket.send_json(message)


async def _receive(websocket: WebSocket, connection: TabConnection) -> None:
    while True:
        message = await _message(websocket)
        if message == {"type": "ping"}:
            await websocket.send_json({"type": "pong"})
            continue
        try:
            reply = ApplicationTabReply.model_validate(message)
        except ValidationError:
            continue
        _service(websocket).receive(reply, connection)


@router.websocket("/application-tabs/bridge")
async def bridge(websocket: WebSocket) -> None:
    """Carry focus commands and replies over the extension's tab socket.

    The socket closes with code 1008 when the hello or a message is invalid.
    The binding is detached when the socket ends.

    Args:
        websocket: Socket opened by the Jobfeed extension.
    """
    connection = None
    hello = None
    tasks: list[asyncio.Task[None]] = []
    try:
        origin = websocket.headers.get("origin", "")
        _extension(origin)
        await websocket.accept()
        hello = ApplicationTabHello.model_validate(
            await asyncio.wait_for(_message(websocket), timeout=5)
        )
        connection = _service(websocket).attach(hello, origin)
        await websocket.send_json({"type": "ready", "binding_id": hello.binding_id})
        tasks = [
            asyncio.create_task(_send(websocket, connection)),
            asyncio.create_task(_receive(websocket, connection)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (ApplicationTabError, ValidationError, TimeoutError, ValueError):
        await websocket.close(code=1008)
    except WebSocketDisconnect:
        return
    finally:
        if connection is not None and hello is not None:
            _service(websocket).detach(hello.binding_id, connection)
        for task in tasks:
            task.cancel()
        if tasks:
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
