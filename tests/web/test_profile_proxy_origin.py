"""Profile saves through the local HTTP/2 proxy retain the origin boundary."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jobfeed.config import Settings
from jobfeed.config_editor import ConfigurationEditor
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.my_profile_preferences import router

HTTP_OK = 200
HTTP_FORBIDDEN = 403


def _app(root: Path) -> ProxyHeadersMiddleware:
    app = FastAPI()
    install_error_handling(app)
    app.state.onboarding_root = root
    app.state.configuration_editor = ConfigurationEditor(
        root / "config.toml", Settings(), lambda _: None
    )
    app.include_router(router, prefix="/api")
    return ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"])


def _headers(scheme: str = "https") -> dict[str, str]:
    return {
        "Origin": f"{scheme}://jobfeed.localhost:1355",
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": scheme,
        "X-Forwarded-For": "::1",
    }


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("backend", ["127.0.0.1:4654", "localhost:4654", "[::1]:4654"])
async def test_preferences_save_through_local_proxy(
    tmp_path: Path, scheme: str, backend: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tmp_path)),
        base_url=f"http://{backend}",
        headers=_headers(scheme),
    ) as client:
        initial = await client.get("/api/my-profile/preferences")
        profile = initial.json()["profile"]
        profile["desired_titles"] = ["Designer"]
        saved = await client.put("/api/my-profile/preferences", json=profile)
        reread = await client.get("/api/my-profile/preferences")
    assert saved.status_code == HTTP_OK, saved.text
    assert reread.json()["profile"]["desired_titles"] == ["Designer"]
    assert (tmp_path / "config.toml").is_file()


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Origin", "https://example.test"),
        ("Origin", "http://127.0.0.1:8765"),
        ("Origin", "https://jobfeed.localhost:1356"),
        ("Origin", "https://jobfeed.localhost:1355/"),
        ("Origin", "https://jobfeed.localhost:1355, https://example.test"),
        ("Origin", "null"),
        ("Origin", ""),
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
        ("Host", "example.test:4654"),
        ("Host", "jobfeed.localhost:4654"),
        ("Host", "127.0.0.1"),
        ("Host", "127.0.0.1:0"),
        ("Host", "127.0.0.1:bad"),
        ("Host", "127.0.0.1:4654,example.test"),
        ("Host", "user@127.0.0.1:4654"),
    ],
)
async def test_proxy_profile_rejects_invalid_forwarding(
    tmp_path: Path, header: str, value: str
) -> None:
    headers = {**_headers(), header: value}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tmp_path)),
        base_url="http://127.0.0.1:4654",
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        response = await client.put(
            "/api/my-profile/preferences", json=profile, headers=headers
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "data/onboarding-resume.json").exists()


async def test_external_peer_cannot_forge_local_forwarding(tmp_path: Path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(tmp_path), client=("203.0.113.10", 12345)
        ),
        base_url="http://127.0.0.1:4654",
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        response = await client.put(
            "/api/my-profile/preferences", json=profile, headers=_headers()
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize(
    "header", ["X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-For"]
)
async def test_proxy_profile_rejects_duplicate_forwarding(
    tmp_path: Path, header: str
) -> None:
    headers = [*_headers().items(), (header, _headers()[header])]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tmp_path)),
        base_url="http://127.0.0.1:4654",
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        response = await client.put(
            "/api/my-profile/preferences", json=profile, headers=headers
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert not (tmp_path / "config.toml").exists()
