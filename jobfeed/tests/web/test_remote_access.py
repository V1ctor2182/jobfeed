"""Tunnel visitors need the access code; requests on this machine never do."""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfeed.cli import cli
from jobfeed.remote_access import RemoteAccessStore, normalize_code
from jobfeed.web.app import create_web_app
from jobfeed.web.remote_access_gate import COOKIE

TUNNEL = {"host": "quiet-river.trycloudflare.com", "cf-connecting-ip": "203.0.113.9"}
TUNNEL_ORIGIN = "https://quiet-river.trycloudflare.com"
CODE_CHARACTERS = 16
OWNER_ONLY = 0o600


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    web_app = create_web_app()
    with TestClient(web_app):
        yield web_app


@pytest.fixture
def store(tmp_path: Path) -> RemoteAccessStore:
    return RemoteAccessStore(tmp_path / "data" / "remote-access.json")


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_local_requests_never_need_the_code(
    app: FastAPI, store: RemoteAccessStore
) -> None:
    store.create()
    assert _client(app).get("/api/health").status_code == HTTPStatus.OK


def test_tunnel_requests_are_refused_while_remote_access_is_off(app: FastAPI) -> None:
    response = _client(app).get("/api/health", headers=TUNNEL)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "turned off" in response.text


@pytest.mark.parametrize(
    "headers",
    [
        TUNNEL,
        {"host": "box.tail1234.ts.net", "tailscale-funnel-request": "?1"},
        {"host": "box.tail1234.ts.net", "x-forwarded-for": "100.64.0.7"},
    ],
)
def test_tunnel_visitors_must_sign_in(
    app: FastAPI, store: RemoteAccessStore, headers: dict[str, str]
) -> None:
    store.create()
    client = _client(app)
    assert (
        client.get("/api/health", headers=headers).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    page = client.get("/", headers=headers)
    assert page.status_code == HTTPStatus.SEE_OTHER
    assert page.headers["location"] == "/access"


def test_the_code_opens_the_dashboard_until_it_is_replaced(
    app: FastAPI, store: RemoteAccessStore
) -> None:
    code = store.create()
    client = _client(app)
    assert "Access code" in client.get("/access", headers=TUNNEL).text

    wrong = client.post("/access", headers=TUNNEL, data={"code": "AAAA-AAAA-AAAA-AAAA"})
    assert wrong.status_code == HTTPStatus.UNAUTHORIZED
    assert COOKIE not in wrong.cookies

    signed_in = client.post(
        "/access",
        headers={**TUNNEL, "origin": TUNNEL_ORIGIN, "x-forwarded-proto": "https"},
        data={"code": code.lower().replace("-", " ")},
    )
    assert signed_in.status_code == HTTPStatus.SEE_OTHER
    cookie = signed_in.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    token = signed_in.cookies[COOKIE]
    session = {**TUNNEL, "cookie": f"{COOKIE}={token}"}
    assert client.get("/api/health", headers=session).status_code == HTTPStatus.OK

    store.create()
    assert (
        client.get("/api/health", headers=session).status_code
        == HTTPStatus.UNAUTHORIZED
    )


def test_sign_in_from_another_site_is_refused(
    app: FastAPI, store: RemoteAccessStore
) -> None:
    code = store.create()
    response = _client(app).post(
        "/access",
        headers={**TUNNEL, "origin": "https://evil.example"},
        data={"code": code},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert COOKIE not in response.cookies


def test_cli_turns_remote_access_on_and_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: RemoteAccessStore
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    runner = CliRunner()
    on = runner.invoke(cli, ["remote-access", "on"], catch_exceptions=False)
    assert on.exit_code == 0, on.output
    code = on.output[on.output.index('"code": "') + 9 :].split('"', 1)[0]
    access = store.load()
    assert access is not None
    assert access.accepts(code)
    assert len(normalize_code(code)) == CODE_CHARACTERS
    assert (store.path.stat().st_mode & 0o777) == OWNER_ONLY
    assert code not in store.path.read_text(encoding="utf-8")

    off = runner.invoke(cli, ["remote-access", "off"], catch_exceptions=False)
    assert off.exit_code == 0, off.output
    assert store.load() is None
