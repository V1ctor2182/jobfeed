"""Explicit application-tab pairing and focus over the real HTTP and WS routes."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jobfeed.application_tabs import ApplicationTabs
from jobfeed.services.application_tab_models import ApplicationTabHello
from jobfeed.web.app import build_web_app
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.application_tabs import router
from tests.web.test_app_skeleton import fake_context

EXTENSION = "chrome-extension://" + "a" * 32
OWNER = "owner-one"
TAB_ID = 1590023306
URL = "https://jobs.ashbyhq.com/example/application"
ORIGIN = "http://localhost:7654"
OK = 200
FORBIDDEN = 403
CONFLICT = 409
UNAVAILABLE = 503
TIMEOUT = 504
AUTH_CLOSE = 1008
TOO_LARGE = 413


@pytest.fixture
def tab_app(tmp_path: Path) -> FastAPI:
    root = tmp_path / "pipeline"
    pipeline = root / ".pipeline"
    batches = pipeline / "batches"
    batches.mkdir(parents=True)
    manifest = batches / "active.json"
    manifest.write_text(json.dumps({"ordered_candidates": [{"id": 1}]}))
    db_path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE application_queue(job_id INTEGER, "
            "codex_application_session_id TEXT)"
        )
        db.execute("INSERT INTO application_queue VALUES(1,?)", (OWNER,))
    with sqlite3.connect(pipeline / "execution-gates.sqlite3") as db:
        db.execute(
            "CREATE TABLE execution_bindings(scope TEXT,job_id INTEGER,owner TEXT)"
        )
        db.execute(
            "INSERT INTO execution_bindings VALUES(?,1,?)", (str(manifest), OWNER)
        )
    with sqlite3.connect(pipeline / "coordination.sqlite3") as db:
        db.execute(
            "CREATE TABLE coordination_bindings(scope TEXT,job_id INTEGER,owner TEXT)"
        )
        db.execute(
            "CREATE TABLE coordination_tabs(id TEXT,scope TEXT,job_id INTEGER,"
            "owner TEXT,browser_id TEXT,tab_id TEXT,origin TEXT,state TEXT)"
        )
        db.execute(
            "INSERT INTO coordination_bindings VALUES(?,1,?)", (str(manifest), OWNER)
        )
        db.execute(
            "INSERT INTO coordination_tabs VALUES('record-one',?,1,?,'1',?,'https://jobs.ashbyhq.com','preserved')",
            (str(manifest), OWNER, str(TAB_ID)),
        )
    app = FastAPI()
    app.state.application_tabs = ApplicationTabs(root, db_path)
    install_error_handling(app)
    app.include_router(router, prefix="/api")
    return app


def headers(client: TestClient, origin: str = ORIGIN) -> dict[str, str]:
    state = client.get("/api/application-tabs")
    assert state.status_code == OK
    return {"Origin": origin, "X-Application-Tabs-CSRF": state.json()["csrf_token"]}


def pair(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/application-tabs/1/pairing",
        json={"owner_thread_id": OWNER},
        headers=headers(client),
    )
    assert response.status_code == OK, response.text
    return response.json()


def connect(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/application-tabs/connect",
        json={
            "code": pair(client)["code"],
            "browser_session_id": "browser-session",
            "native_tab_id": TAB_ID,
            "url": URL,
        },
        headers={"Origin": EXTENSION},
    )
    assert response.status_code == OK, response.text
    return response.json()


def hello(credentials: dict[str, str]) -> dict[str, str]:
    return {"type": "hello", **credentials, "browser_session_id": "browser-session"}


def request_focus(client: TestClient, credentials: dict[str, str]):
    return client.post(
        "/api/application-tabs/1/focus",
        json={"owner_thread_id": OWNER, "binding_id": credentials["binding_id"]},
        headers=headers(client),
    )


def test_pair_connect_focus_and_heartbeat(tab_app: FastAPI) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            assert ws.receive_json() == {
                "type": "ready",
                "binding_id": credentials["binding_id"],
            }
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            assert (
                client.get("/api/application-tabs").json()["bindings"][0]["state"]
                == "connected"
            )
            with ThreadPoolExecutor() as pool:
                response = pool.submit(request_focus, client, credentials)
                command = ws.receive_json()
                assert command["type"] == "focus"
                assert command["binding_id"] == credentials["binding_id"]
                assert command["expires_at"] > int(datetime.now(UTC).timestamp() * 1000)
                ws.send_json(
                    {
                        "type": "focus_result",
                        "request_id": command["request_id"],
                        "binding_id": credentials["binding_id"],
                        "status": "focused",
                    }
                )
                assert response.result(timeout=2).json() == {"status": "focused"}
        assert request_focus(client, credentials).status_code == UNAVAILABLE


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:7654",
        "http://127.0.0.1:7654",
        "http://[::1]:7654",
        "https://jobfeed.localhost:1355",
    ],
)
def test_dashboard_csrf_allows_only_same_local_origin(
    tab_app: FastAPI, origin: str
) -> None:
    with TestClient(tab_app, base_url=origin) as client:
        good = headers(client, origin)
        body = {"owner_thread_id": OWNER}
        endpoint = "/api/application-tabs/1/pairing"
        assert client.post(endpoint, json=body, headers=good).status_code == OK
        assert (
            client.post(endpoint, json=body, headers={"Origin": origin}).status_code
            == FORBIDDEN
        )
        assert (
            client.post(
                endpoint, json=body, headers={**good, "Origin": "https://evil.example"}
            ).status_code
            == FORBIDDEN
        )
        assert (
            client.post(
                endpoint,
                json=body,
                headers={
                    **good,
                    "Origin": "https://jobfeed.localhost.evil.example:1355",
                },
            ).status_code
            == FORBIDDEN
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://jobfeed.localhost:8888",
        "http://localhost.evil.example:7654",
    ],
)
def test_dns_rebinding_hosts_cannot_read_csrf(tab_app: FastAPI, origin: str) -> None:
    with TestClient(tab_app, base_url=origin) as client:
        assert client.get("/api/application-tabs").status_code == FORBIDDEN


def test_pairing_codes_are_one_use_and_expire(tab_app: FastAPI) -> None:
    service = tab_app.state.application_tabs
    now = datetime.now(UTC)
    service.now = lambda: now
    with TestClient(tab_app, base_url=ORIGIN) as client:
        code = pair(client)["code"]
        body = {
            "code": code,
            "browser_session_id": "session",
            "native_tab_id": TAB_ID,
            "url": URL,
        }
        assert (
            client.post(
                "/api/application-tabs/connect",
                json=body,
                headers={"Origin": EXTENSION},
            ).status_code
            == OK
        )
        assert (
            client.post(
                "/api/application-tabs/connect",
                json=body,
                headers={"Origin": EXTENSION},
            ).status_code
            == FORBIDDEN
        )
        body["code"] = pair(client)["code"]
        now += timedelta(minutes=5)
        assert (
            client.post(
                "/api/application-tabs/connect",
                json=body,
                headers={"Origin": EXTENSION},
            ).status_code
            == FORBIDDEN
        )


@pytest.mark.parametrize(
    "change",
    [{"native_tab_id": TAB_ID + 1}, {"url": "https://different.example/application"}],
)
def test_connect_requires_exact_recorded_tab_and_origin(
    tab_app: FastAPI, change: dict[str, object]
) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        body = {
            "code": pair(client)["code"],
            "browser_session_id": "session",
            "native_tab_id": TAB_ID,
            "url": URL,
            **change,
        }
        assert (
            client.post(
                "/api/application-tabs/connect",
                json=body,
                headers={"Origin": EXTENSION},
            ).status_code
            == CONFLICT
        )
        assert (
            client.post(
                "/api/application-tabs/connect", json=body, headers={"Origin": ORIGIN}
            ).status_code
            == FORBIDDEN
        )


@pytest.mark.parametrize(
    "change", [{"session_token": "wrong"}, {"browser_session_id": "wrong"}]
)
def test_websocket_rejects_wrong_session(
    tab_app: FastAPI, change: dict[str, str]
) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json({**hello(credentials), **change})
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == AUTH_CLOSE


@pytest.mark.parametrize("status", ["tab_closed", "page_changed", "focus_failed"])
def test_focus_results_preserve_failure_status(tab_app: FastAPI, status: str) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            with ThreadPoolExecutor() as pool:
                response = pool.submit(request_focus, client, credentials)
                command = ws.receive_json()
                ws.send_json(
                    {
                        "type": "focus_result",
                        "request_id": command["request_id"],
                        "binding_id": credentials["binding_id"],
                        "status": status,
                    }
                )
                assert response.result(timeout=2).json() == {"status": status}
            if status != "focus_failed":
                assert (
                    client.get("/api/application-tabs").json()["bindings"][0]["state"]
                    == status
                )


def test_wrong_reply_ids_cannot_confirm_focus(tab_app: FastAPI) -> None:
    tab_app.state.application_tabs.focus_timeout = 0.1
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            with ThreadPoolExecutor() as pool:
                response = pool.submit(request_focus, client, credentials)
                command = ws.receive_json()
                ws.send_json(
                    {
                        "type": "focus_result",
                        "request_id": "wrong",
                        "binding_id": credentials["binding_id"],
                        "status": "focused",
                    }
                )
                ws.send_json(
                    {
                        "type": "focus_result",
                        "request_id": command["request_id"],
                        "binding_id": "wrong",
                        "status": "focused",
                    }
                )
                result = response.result(timeout=2)
                assert result.status_code == TIMEOUT
                assert "unconfirmed" in result.json()["error"]["code"]


def test_disconnect_cancels_pending_focus(tab_app: FastAPI) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            with ThreadPoolExecutor() as pool:
                response = pool.submit(request_focus, client, credentials)
                ws.receive_json()
                ws.close()
                assert response.result(timeout=2).status_code == UNAVAILABLE


@pytest.mark.parametrize("revocation", ["owner", "closed", "origin", "scope"])
def test_focus_rechecks_owner_and_recorded_tab(
    tab_app: FastAPI, revocation: str
) -> None:
    service = tab_app.state.application_tabs
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            if revocation == "owner":
                with sqlite3.connect(service.db_path) as db:
                    db.execute(
                        "UPDATE application_queue "
                        "SET codex_application_session_id='new-owner'"
                    )
            elif revocation == "scope":
                with sqlite3.connect(
                    service.root / ".pipeline/execution-gates.sqlite3"
                ) as db:
                    db.execute("UPDATE execution_bindings SET scope='missing-scope'")
            else:
                with sqlite3.connect(
                    service.root / ".pipeline/coordination.sqlite3"
                ) as db:
                    db.execute(
                        "UPDATE coordination_tabs SET state='closed'"
                        if revocation == "closed"
                        else "UPDATE coordination_tabs SET origin='https://different.example'"
                    )
            assert request_focus(client, credentials).status_code == CONFLICT
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == AUTH_CLOSE


@pytest.mark.parametrize("revocation", ["owner", "closed"])
def test_connect_rechecks_evidence_after_pairing(
    tab_app: FastAPI, revocation: str
) -> None:
    service = tab_app.state.application_tabs
    with TestClient(tab_app, base_url=ORIGIN) as client:
        code = pair(client)["code"]
        path = (
            service.db_path
            if revocation == "owner"
            else service.root / ".pipeline/coordination.sqlite3"
        )
        with sqlite3.connect(path) as db:
            if revocation == "owner":
                db.execute(
                    "UPDATE application_queue SET codex_application_session_id='new'"
                )
            else:
                db.execute("UPDATE coordination_tabs SET state='closed'")
        response = client.post(
            "/api/application-tabs/connect",
            json={
                "code": code,
                "browser_session_id": "session",
                "native_tab_id": TAB_ID,
                "url": URL,
            },
            headers={"Origin": EXTENSION},
        )
        assert response.status_code == CONFLICT
        assert client.get("/api/application-tabs").json()["bindings"] == []


def test_extension_origin_is_bound_and_duplicate_connection_rejected(
    tab_app: FastAPI,
) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge",
            headers={"Origin": "chrome-extension://" + "b" * 32},
        ) as wrong:
            wrong.send_json(hello(credentials))
            with pytest.raises(WebSocketDisconnect) as error:
                wrong.receive_json()
            assert error.value.code == AUTH_CLOSE
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as first:
            first.send_json(hello(credentials))
            first.receive_json()
            with client.websocket_connect(
                "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
            ) as second:
                second.send_json(hello(credentials))
                with pytest.raises(WebSocketDisconnect) as error:
                    second.receive_json()
                assert error.value.code == AUTH_CLOSE
            first.send_json({"type": "ping"})
            assert first.receive_json() == {"type": "pong"}


def test_repairing_revokes_original_socket(tab_app: FastAPI) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            replacement = connect(client)
            assert replacement["binding_id"] != credentials["binding_id"]
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == AUTH_CLOSE


def test_stale_disconnect_cannot_clear_new_connection(tab_app: FastAPI) -> None:
    service = tab_app.state.application_tabs
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
    message = ApplicationTabHello.model_validate(hello(credentials))
    first = service.attach(message, EXTENSION)
    service.detach(credentials["binding_id"], first)
    second = service.attach(message, EXTENSION)
    service.detach(credentials["binding_id"], first)
    assert service.bindings[credentials["binding_id"]].connection is second
    assert service.state().bindings[0].state == "connected"


def test_one_pending_focus_and_late_reply_is_ignored(tab_app: FastAPI) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        credentials = connect(client)
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_json(hello(credentials))
            ws.receive_json()
            with ThreadPoolExecutor() as pool:
                pending = pool.submit(request_focus, client, credentials)
                command = ws.receive_json()
                assert request_focus(client, credentials).status_code == CONFLICT
                reply = {
                    "type": "focus_result",
                    "request_id": command["request_id"],
                    "binding_id": credentials["binding_id"],
                    "status": "focused",
                }
                ws.send_json(reply)
                assert pending.result(timeout=2).json() == {"status": "focused"}
                next_pending = pool.submit(request_focus, client, credentials)
                next_command = ws.receive_json()
                ws.send_json(reply)
                ws.send_json(
                    {
                        **reply,
                        "request_id": next_command["request_id"],
                        "status": "focus_failed",
                    }
                )
                assert next_pending.result(timeout=2).json() == {
                    "status": "focus_failed"
                }


def test_web_app_wires_ephemeral_service(
    tab_app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(tab_app.state.application_tabs.root))
    app = build_web_app(fake_context(), static_dir=tmp_path / "no-spa")
    assert isinstance(app.state.application_tabs, ApplicationTabs)
    assert app.state.application_tabs.root == tab_app.state.application_tabs.root
    assert app.state.application_tabs.state().bindings == []
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/application-tabs")
        assert response.status_code == OK
        assert response.headers["cache-control"] == "no-store"


def test_pairing_does_not_write_runtime_evidence(tab_app: FastAPI) -> None:
    service = tab_app.state.application_tabs
    paths = [service.db_path, *service.root.rglob("*")]
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    with TestClient(tab_app, base_url=ORIGIN) as client:
        connect(client)
    assert {path: path.read_bytes() for path in before} == before
    assert [path for path in service.root.rglob("*") if path.is_file()] == [
        path for path in paths[1:] if path.is_file()
    ]


def test_http_and_websocket_messages_are_bounded(tab_app: FastAPI) -> None:
    with TestClient(tab_app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/application-tabs/1/pairing",
            json={"owner_thread_id": "x" * 9000},
            headers=headers(client),
        )
        assert response.status_code == TOO_LARGE
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as ws:
            ws.send_text("x" * 9000)
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == AUTH_CLOSE


async def test_pairing_accepts_verified_portless_proxy_with_current_csrf(tab_app):
    service = tab_app.state.application_tabs
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tab_app), base_url="http://127.0.0.1:4654"
    ) as client:
        response = await client.post(
            "/api/application-tabs/1/pairing",
            json={"owner_thread_id": OWNER},
            headers={
                "Origin": "https://jobfeed.localhost:1355",
                "X-Forwarded-Host": "jobfeed.localhost:1355",
                "X-Forwarded-Proto": "https",
                "X-Application-Tabs-CSRF": service.csrf_token,
            },
        )
    assert response.status_code == OK, response.text
    assert response.json()["code"] in service.pairings


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Origin", "https://evil.example"),
        ("Origin", "https://jobfeed.localhost:1356"),
        ("Host", "127.0.0.1:9999"),
        ("X-Forwarded-Host", "evil.example"),
        ("X-Forwarded-Proto", "http"),
        ("X-Forwarded-For", "203.0.113.1"),
        ("X-Forwarded-For", "::1, 127.0.0.1"),
        ("X-Application-Tabs-CSRF", "stale-token"),
    ],
)
async def test_proxy_pairing_refuses_foreign_origins_spoofing_and_stale_tokens(
    tab_app, header, value
):

    request_headers = {
        "Origin": "https://jobfeed.localhost:1355",
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": "https",
        "X-Application-Tabs-CSRF": tab_app.state.application_tabs.csrf_token,
        header: value,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ProxyHeadersMiddleware(tab_app)),
        base_url="http://127.0.0.1:4654",
    ) as client:
        response = await client.post(
            "/api/application-tabs/1/pairing",
            json={"owner_thread_id": OWNER},
            headers=request_headers,
        )
    assert response.status_code == FORBIDDEN
    assert tab_app.state.application_tabs.pairings == {}
