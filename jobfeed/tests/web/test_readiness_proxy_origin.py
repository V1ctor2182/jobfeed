"""The proxy UI uses the backend socket for bounded HTTP-only diagnostics."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jobfeed.config import Settings
from jobfeed.web.routes.apply_readiness import router as readiness_router
from jobfeed.web.routes.onboarding_checklist import router as checklist_router
from tests.web.test_apply_readiness import RUN_ID, FakeReadiness
from tests.web.test_onboarding_checklist import _inputs

HTTP_OK = 200
HTTP_FORBIDDEN = 403
BACKEND = "http://127.0.0.1:4654"
PUBLIC = "https://jobfeed.localhost:1355"
CSRF = "synthetic-csrf-token"


def _headers(host: str = "127.0.0.1:4654") -> dict[str, str]:
    return {
        "Host": host,
        "Origin": PUBLIC,
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "::1",
        "X-Readiness-CSRF": CSRF,
    }


def _app() -> tuple[ProxyHeadersMiddleware, FakeReadiness]:
    app = FastAPI()
    app.include_router(readiness_router, prefix="/api")
    app.state.readiness_csrf_token = CSRF
    provider = FakeReadiness()
    app.state.apply_readiness = provider
    return ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"]), provider


@pytest.mark.parametrize("host", ["jobfeed.localhost:1355", "127.0.0.1:4654"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_proxy_readiness_uses_backend_for_http1_and_http2(
    host: str, scheme: str
) -> None:
    app, provider = _app()
    headers = {
        **_headers(host),
        "Origin": f"{scheme}://jobfeed.localhost:1355",
        "X-Forwarded-Proto": scheme,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BACKEND
    ) as client:
        response = await client.post(
            "/api/apply-readiness", headers=headers, json={"request_id": RUN_ID}
        )
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["run_id"] == RUN_ID
    assert provider.starts == [(RUN_ID, "http://127.0.0.1:4654")]


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Origin", "https://example.test"),
        ("Origin", "https://jobfeed.localhost:1356"),
        ("Origin", "https://jobfeed.localhost:1355/"),
        ("Origin", "null"),
        ("Origin", ""),
        ("Host", "example.test:4654"),
        ("Host", "127.0.0.1:8765"),
        ("X-Forwarded-Host", "example.test"),
        ("X-Forwarded-Host", "jobfeed.localhost:1356"),
        ("X-Forwarded-Host", "jobfeed.localhost:1355, example.test"),
        ("X-Forwarded-Host", ""),
        ("X-Forwarded-Proto", "http"),
        ("X-Forwarded-Proto", "https,http"),
        ("X-Forwarded-Proto", ""),
        ("X-Forwarded-For", "203.0.113.10"),
        ("X-Forwarded-For", "203.0.113.10, 127.0.0.1"),
        ("X-Forwarded-For", "::1, 127.0.0.1"),
        ("X-Readiness-CSRF", "wrong"),
        ("X-Readiness-CSRF", ""),
    ],
)
async def test_invalid_proxy_readiness_never_admits_work(
    header: str, value: str
) -> None:
    app, provider = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BACKEND
    ) as client:
        response = await client.post(
            "/api/apply-readiness",
            headers={**_headers(), header: value},
            json={"request_id": RUN_ID},
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert provider.starts == []


@pytest.mark.parametrize(
    "header",
    ["Origin", "Host", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-For"],
)
async def test_duplicate_proxy_headers_cannot_admit_work(header: str) -> None:
    app, provider = _app()
    headers = [*_headers().items(), (header, _headers()[header])]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BACKEND
    ) as client:
        response = await client.post(
            "/api/apply-readiness", headers=headers, json={"request_id": RUN_ID}
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert provider.starts == []


@pytest.mark.parametrize(
    ("client_host", "base_url"),
    [("203.0.113.10", BACKEND), ("127.0.0.1", "http://203.0.113.10:4654")],
)
async def test_external_peer_or_socket_cannot_forge_proxy(
    client_host: str, base_url: str
) -> None:
    app, provider = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_host, 12345)),
        base_url=base_url,
    ) as client:
        response = await client.post(
            "/api/apply-readiness", headers=_headers(), json={"request_id": RUN_ID}
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert provider.starts == []


def _checklist_app(tmp_path: Path) -> tuple[ProxyHeadersMiddleware, Path]:
    inputs = _inputs(tmp_path)
    database = tmp_path / "codex.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ("synthetic-relay", str(tmp_path), "synthetic-rollout"),
        )
    registration = inputs.apply_root / ".pipeline/claude-batch-bridge.sqlite3"
    registration.parent.mkdir()
    config = {
        "root": str(inputs.apply_root),
        "project_root": str(tmp_path),
        "policy": "jobfeed-existing-resume-batch-v2",
        "jobfeed_db": str(inputs.database),
        "api_url": BACKEND,
        "project_id": "synthetic-project",
        "bootstrap_relay": "synthetic-relay",
        "codex_db": str(database),
    }
    with sqlite3.connect(registration) as connection:
        connection.execute("CREATE TABLE scopes (config TEXT)")
        connection.execute("INSERT INTO scopes VALUES (?)", (json.dumps(config),))
    app = FastAPI()
    app.include_router(checklist_router, prefix="/api")
    app.state.onboarding_root = inputs.root
    app.state.context = {"settings": Settings(db={"path": inputs.database})}
    app.state.apply_health_reader = SimpleNamespace(root=inputs.apply_root)
    app.state.configuration_editor = SimpleNamespace(editable=inputs.active)
    app.state.onboarding_provider_service = SimpleNamespace(
        state=lambda: inputs.provider
    )
    app.state.onboarding_resume_service = SimpleNamespace(state=lambda: inputs.resume)
    app.state.apply_readiness = SimpleNamespace(get=lambda: None)
    return ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"]), registration


@pytest.mark.parametrize("host", ["jobfeed.localhost:1355", "127.0.0.1:4654"])
async def test_checklist_matches_backend_registration_through_proxy(
    tmp_path: Path, host: str
) -> None:
    app, registration = _checklist_app(tmp_path)
    before = registration.read_bytes()
    headers = _headers(host)
    del headers["Origin"]
    del headers["X-Readiness-CSRF"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BACKEND
    ) as client:
        response = await client.get("/api/onboarding/checklist", headers=headers)
    assert response.status_code == HTTP_OK
    checks = {
        check["id"]: check
        for section in response.json()["sections"]
        for check in section["checks"]
    }
    assert checks["batch_setup"]["status"] == "ready"
    assert "connectivity remain unverified" in checks["batch_setup"]["detail"]
    assert registration.read_bytes() == before
