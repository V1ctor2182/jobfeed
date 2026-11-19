"""Profile preference API contract with real local draft and settings persistence."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.config import Settings
from jobfeed.config_editor import ConfigurationEditor
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.configuration import router as configuration_router
from jobfeed.web.routes.my_profile_preferences import router

HTTP_OK = 200
HTTP_UNPROCESSABLE = 422
HTTP_FORBIDDEN = 403


def _app(root: Path) -> FastAPI:
    app = FastAPI()
    install_error_handling(app)
    app.state.onboarding_root = root
    app.state.configuration_editor = ConfigurationEditor(
        root / "config.toml", Settings(), lambda _: None
    )
    app.include_router(router, prefix="/api")
    app.include_router(configuration_router, prefix="/api")
    return app


async def test_edit_preferences_without_model_or_resume(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:8765"},
    ) as client:
        initial = await client.get("/api/my-profile/preferences")
        assert initial.status_code == HTTP_OK
        assert initial.headers["Cache-Control"] == "no-store"
        profile = initial.json()["profile"]
        profile.update(
            {
                "desired_titles": ["Designer"],
                "target_locations": ["Boston"],
                "excluded_companies": ["Example Company"],
            }
        )
        saved = await client.put("/api/my-profile/preferences", json=profile)
        reread = await client.get("/api/my-profile/preferences")

    assert saved.status_code == HTTP_OK
    assert saved.headers["Cache-Control"] == "no-store"
    assert saved.json() == reread.json()
    assert saved.json()["profile"]["desired_titles"] == ["Designer"]
    assert saved.json()["searches_need_review"] is True
    assert saved.json()["scoring_resume_active"] is False
    active = app.state.configuration_editor.editable
    assert active.hard_filters.location_allowlist == ["Boston"]
    assert active.hard_filters.company_blocklist == ["Example Company"]


async def test_invalid_preferences_cannot_write_config(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:8765"},
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        profile["maximum_posting_age_hours"] = 0
        response = await client.put("/api/my-profile/preferences", json=profile)
    assert response.status_code == HTTP_UNPROCESSABLE
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "data/onboarding-resume.json").exists()


@pytest.mark.parametrize(
    "origin", [None, "https://example.com", "http://localhost:8765"]
)
async def test_cross_origin_preferences_cannot_write_config(
    tmp_path: Path, origin: str | None
) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        headers = {"Origin": origin} if origin is not None else {}
        response = await client.put(
            "/api/my-profile/preferences", json=profile, headers=headers
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "data/onboarding-resume.json").exists()


async def test_preferences_save_from_https_local_dashboard_proxy(
    tmp_path: Path,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tmp_path)),
        base_url="http://jobfeed.localhost:1355",
    ) as client:
        before = await client.get("/api/my-profile/preferences")
        saved = await client.put(
            "/api/my-profile/preferences",
            headers={"Origin": "https://jobfeed.localhost:1355"},
            json=before.json()["profile"],
        )
    assert saved.status_code == HTTP_OK


async def test_profile_rereads_active_filters_then_preserves_them_on_unrelated_edit(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:8765"},
    ) as client:
        profile = (await client.get("/api/my-profile/preferences")).json()["profile"]
        profile.update(target_countries=["United States"], target_locations=["Boston"])
        first_save = await client.put("/api/my-profile/preferences", json=profile)
        assert first_save.status_code == HTTP_OK

        latest = app.state.configuration_editor.editable
        latest.hard_filters.company_blocklist = ["Haystack"]
        latest.hard_filters.location_allowlist = []
        app.state.configuration_editor.save(latest)

        current = (await client.get("/api/my-profile/preferences")).json()["profile"]
        assert current["excluded_companies"] == ["Haystack"]
        assert current["target_locations"] == []
        assert current["target_countries"] == []
        current["hiring_timeline"] = "Next month"
        saved = await client.put("/api/my-profile/preferences", json=current)
        config = (await client.get("/api/config")).json()

    assert saved.status_code == HTTP_OK, saved.text
    assert config["hard_filters"]["company_blocklist"] == ["Haystack"]
    assert config["hard_filters"]["location_allowlist"] == []
