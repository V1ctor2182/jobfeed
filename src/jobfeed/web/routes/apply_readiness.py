"""Start explicit loopback-only diagnostics and observe their safe results."""

from __future__ import annotations

import json
import secrets
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from jobfeed.services.readiness_models import (
    ReadinessProvider,
    ReadinessRun,
    ReadinessSnapshot,
)
from jobfeed.web.dashboard_origin import dashboard_loopback_origin

router = APIRouter()
_MAX_BODY = 128


class _StartRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str


def _provider(request: Request) -> ReadinessProvider:
    return cast(ReadinessProvider, request.app.state.apply_readiness)


def _mutation_origin(request: Request) -> str:
    origin = dashboard_loopback_origin(
        request, browser_origin=request.headers.get("origin", "")
    )
    token = request.headers.get("x-readiness-csrf", "")
    if origin is None or not secrets.compare_digest(
        token, request.app.state.readiness_csrf_token
    ):
        raise HTTPException(403, "Use this dashboard's loopback readiness button.")
    return origin


@router.get("/apply-readiness", response_model=ReadinessSnapshot)
def get_apply_readiness(request: Request) -> ReadinessSnapshot:
    """Observe the latest diagnostic without dispatching one.

    Args:
        request: Current dashboard request.

    Returns:
        Safe status plus the per-server mutation token.
    """
    return ReadinessSnapshot(
        csrf_token=request.app.state.readiness_csrf_token,
        run=_provider(request).get(),
    )


@router.post("/apply-readiness", response_model=ReadinessRun)
async def start_apply_readiness(request: Request) -> ReadinessRun:
    """Admit one manual, same-origin, bounded diagnostic request.

    Args:
        request: Dashboard request with token and a strict JSON UUID body.

    Returns:
        The newly admitted or original active diagnostic.

    Raises:
        HTTPException: If the origin, token, body or local setup is invalid.
    """
    origin = _mutation_origin(request)
    if (
        request.headers.get("content-type", "").split(";")[0].strip()
        != "application/json"
        or "transfer-encoding" in request.headers
    ):
        raise HTTPException(415, "A bounded JSON request is required.")
    length = request.headers.get("content-length", "")
    if not length.isdecimal() or int(length) > _MAX_BODY:
        raise HTTPException(413, "Readiness request is too large or unbounded.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY:
            raise HTTPException(413, "Readiness request is too large.")
    try:
        data = _StartRequest.model_validate(json.loads(body))
        if str(UUID(data.request_id)) != data.request_id:
            raise ValueError("noncanonical UUID")
    except (ValueError, ValidationError, TypeError):
        raise HTTPException(422, "Supply only a canonical request_id UUID.") from None
    try:
        return await run_in_threadpool(
            _provider(request).start, data.request_id, origin
        )
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(
            503,
            "Readiness setup is unavailable. Check the local runtime configuration.",
        ) from None


@router.get("/apply-readiness/fixture/{run_id}", response_class=HTMLResponse)
def get_readiness_fixture(request: Request, run_id: str) -> HTMLResponse:
    """Serve a synthetic file chooser with no upload or submission action.

    Args:
        request: Dashboard request.
        run_id: Admitted diagnostic UUID.

    Returns:
        Static fixture with restrictive content policy.

    Raises:
        HTTPException: If the fixture is unknown.
    """
    try:
        html = _provider(request).fixture(run_id)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(404, "Unknown readiness fixture.") from None
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "default-src 'none'; form-action 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; connect-src 'none'",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
