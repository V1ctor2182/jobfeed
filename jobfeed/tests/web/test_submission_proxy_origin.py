from http import HTTPStatus
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.application_submissions import router

BACKEND = "http://127.0.0.1:4654"
PUBLIC = "https://jobfeed.localhost:1355"
TOKEN_HEADER = "X-Application-Submissions-CSRF"
BODY = {
    "request_id": "synthetic-request",
    "expected_preview": {"job_id": 3},
    "confirmed": True,
}


def proxy_headers():
    return {
        "Host": "127.0.0.1:4654",
        "Origin": PUBLIC,
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "::1",
        TOKEN_HEADER: "synthetic-token",
    }


async def post(headers, *, backend=BACKEND, peer="127.0.0.1"):
    app = FastAPI()
    calls = []

    def command(name, payload):
        calls.append((name, payload))
        return {"status": "synthetic-only"}

    app.state.application_submissions = SimpleNamespace(
        csrf_token="synthetic-token", command=command
    )
    install_error_handling(app)
    app.include_router(router, prefix="/api")
    proxy = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy, client=(peer, 12345)),
        base_url=backend,
    ) as client:
        response = await client.post(
            "/api/application-submissions/confirm", headers=headers, json=BODY
        )
    return response, calls


@pytest.mark.parametrize("host", ["jobfeed.localhost:1355", "127.0.0.1:4654"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_verified_portless_confirmation_preserves_exact_payload(host, scheme):
    headers = {
        **proxy_headers(),
        "Host": host,
        "Origin": f"{scheme}://jobfeed.localhost:1355",
        "X-Forwarded-Proto": scheme,
    }
    response, calls = await post(headers)
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("confirm", BODY)]


@pytest.mark.parametrize("host", ["localhost:4654", "127.0.0.1:4654", "[::1]:4654"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_direct_loopback_confirmation_remains_supported(host, scheme):
    origin = f"{scheme}://{host}"
    response, calls = await post(
        {"Origin": origin, TOKEN_HEADER: "synthetic-token"}, backend=origin
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    assert calls == [("confirm", BODY)]


@pytest.mark.parametrize(
    "header,value",
    [
        ("Origin", "https://attacker.test"),
        ("Origin", "null"),
        ("Origin", ""),
        ("Origin", "https://jobfeed.localhost:1356"),
        ("Host", "attacker.test"),
        ("Host", "127.0.0.1:8765"),
        ("X-Forwarded-Host", "attacker.test"),
        ("X-Forwarded-Host", ""),
        ("X-Forwarded-Proto", "http"),
        ("X-Forwarded-Proto", "https,http"),
        ("X-Forwarded-For", "203.0.113.10"),
        ("X-Forwarded-For", "::1, 127.0.0.1"),
        (TOKEN_HEADER, "wrong"),
        (TOKEN_HEADER, ""),
    ],
)
@pytest.mark.parametrize("host", ["jobfeed.localhost:1355", "127.0.0.1:4654"])
async def test_forged_proxy_or_wrong_token_never_confirms(header, value, host):
    response, calls = await post({**proxy_headers(), "Host": host, header: value})
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert calls == []


@pytest.mark.parametrize(
    "header",
    [
        "Origin",
        "Host",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-For",
        TOKEN_HEADER,
    ],
)
async def test_duplicate_proxy_headers_never_confirm(header):
    headers = proxy_headers()
    response, calls = await post([*headers.items(), (header, headers[header])])
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert calls == []


@pytest.mark.parametrize("header", ["Origin", "Host", TOKEN_HEADER])
async def test_duplicate_direct_headers_never_confirm(header):
    headers = {
        "Origin": BACKEND,
        "Host": "127.0.0.1:4654",
        TOKEN_HEADER: "synthetic-token",
    }
    response, calls = await post([*headers.items(), (header, headers[header])])
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert calls == []


@pytest.mark.parametrize("header", ["Origin", TOKEN_HEADER])
async def test_missing_confirmation_headers_never_confirm(header):
    headers = proxy_headers()
    del headers[header]
    response, calls = await post(headers)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert calls == []


@pytest.mark.parametrize(
    "peer,backend",
    [("203.0.113.10", BACKEND), ("127.0.0.1", "http://203.0.113.10:4654")],
)
async def test_remote_peer_or_socket_cannot_forge_proxy(peer, backend):
    response, calls = await post(proxy_headers(), peer=peer, backend=backend)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert calls == []
