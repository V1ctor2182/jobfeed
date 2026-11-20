"""`jobfeed onboard` drives first-run setup through the real dashboard API."""

from __future__ import annotations

import importlib
import json
import tomllib
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner, Result
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfeed.cli.onboard import onboard
from jobfeed.onboarding_types import ProviderOnboardingState
from jobfeed.web.app import create_web_app

# `jobfeed.cli.onboard` as an attribute is the Click group, not the module.
onboard_module = importlib.import_module("jobfeed.cli.onboard")

DASHBOARD = "http://127.0.0.1:7654"
JOBS_PER_RUN = 55
DAILY_CALLS = 70
USAGE_ERROR = 2

pytestmark = pytest.mark.usefixtures("app")

PROFILE = {
    "desired_titles": ["Platform Engineer", "Site Reliability Engineer"],
    "seniority_levels": ["Senior"],
    "target_countries": ["United States"],
    "target_locations": ["New York, NY"],
    "work_modes": ["hybrid"],
    "industries": ["Developer tools"],
    "company_sizes": ["mid-size"],
    "work_authorization": "Authorized",
    "hiring_timeline": "Available now",
    "excluded_titles": [],
    "excluded_companies": [],
    "excluded_locations": [],
    "excluded_keywords": [],
    "maximum_posting_age_hours": 36,
    "resume_evidence": ["Built distributed systems"],
}


class ConnectedProvider:
    """A signed-in CLI provider with both models chosen."""

    def state(self) -> ProviderOnboardingState:
        return ProviderOnboardingState(
            provider="claude_cli",
            connected=True,
            quick_model="quick-model",
            detailed_model="detailed-model",
        )


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    web_app = create_web_app()
    web_app.state.onboarding_provider_service = ConnectedProvider()
    with TestClient(web_app):
        monkeypatch.setattr(
            onboard_module, "_client_factory", partial(_UnclosedClient, web_app)
        )
        yield web_app


class _UnclosedClient(TestClient):
    """Reuse the fixture's running app instead of restarting its lifespan."""

    def __init__(self, app: FastAPI, *, trust_env: bool, **kwargs: Any) -> None:
        assert trust_env is False
        super().__init__(app, **kwargs)

    def __enter__(self) -> _UnclosedClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _run(*args: str, input_text: str | None = None) -> Result:
    return CliRunner().invoke(
        onboard, ["--url", DASHBOARD, *args], input=input_text, catch_exceptions=False
    )


def _json(result: Result) -> object:
    assert result.exit_code == 0, result.output
    # The in-process app logs requests to the same captured stdout.
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if line[:1] in "{[")
    return json.loads("\n".join(lines[start:]))


def test_onboard_completes_setup_end_to_end(tmp_path: Path) -> None:
    """Upload, confirm, choose searches and finish without the setup screens."""
    resume = tmp_path / "resume.md"
    resume.write_text("# Ada\nPlatform engineer.\n", encoding="utf-8")

    uploaded = _json(_run("resume", str(resume), "--no-analyze"))
    assert uploaded == {
        "resume": "resume.md",
        "extracted_characters": uploaded["extracted_characters"],  # type: ignore[index]
        "extracted_preview": uploaded["extracted_preview"],  # type: ignore[index]
        "profile": None,
        "confirmed": False,
    }
    assert "Platform engineer" in uploaded["extracted_preview"]  # type: ignore[index]
    assert _json(_run("status"))["next_step"] == "profile_suggested"  # type: ignore[index]

    confirmed = _json(_run("profile", "--confirm", "-", input_text=json.dumps(PROFILE)))
    assert confirmed["confirmed"] is True  # type: ignore[index]

    searches = _json(_run("searches", "--enable", "platform engineer"))
    assert {(row["title"], row["enabled"]) for row in searches} == {  # type: ignore[union-attr]
        ("Platform Engineer", True),
        ("Site Reliability Engineer", False),
    }

    plan = _json(_run("finish", "--dry-run", "--jobs-per-run", str(JOBS_PER_RUN)))
    assert plan["saved"] is False  # type: ignore[index]
    assert not (tmp_path / "config.toml").exists()

    finished = _json(
        _run(
            "finish",
            "--jobs-per-run",
            str(JOBS_PER_RUN),
            "--daily-calls",
            str(DAILY_CALLS),
        )
    )
    assert finished["saved"] is True  # type: ignore[index]
    saved = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert saved["llm"]["stage_a"] == "claude-cli/quick-model"
    assert saved["llm"]["stage_b"] == "claude-cli/detailed-model"
    assert saved["llm"]["max_daily_score_calls"] == DAILY_CALLS
    assert saved["scoring"]["default_eval_limit"] == JOBS_PER_RUN
    assert saved["sources"]["linkedin_guest"]["enabled"] is True
    assert saved["sources"]["ats"]["title_keywords"] == ["Platform Engineer"]
    assert saved["llm"]["master_resume_path"].startswith("data/resumes/")

    status = _json(_run("status"))
    assert status["next_step"] == "done"  # type: ignore[index]
    assert status["enabled_searches"] == [  # type: ignore[index]
        "linkedin_guest: Platform Engineer",
        "indeed: Platform Engineer",
    ]


def test_unknown_search_title_changes_nothing(tmp_path: Path) -> None:
    resume = tmp_path / "resume.md"
    resume.write_text("Platform engineer.\n", encoding="utf-8")
    _json(_run("resume", str(resume), "--no-analyze"))
    _json(_run("profile", "--confirm", "-", input_text=json.dumps(PROFILE)))

    result = _run("searches", "--enable", "Astronaut")

    assert result.exit_code == USAGE_ERROR
    assert "Platform Engineer" in result.output
    assert not any(row["enabled"] for row in _json(_run("searches")))  # type: ignore[union-attr]


def test_profile_before_upload_explains_next_step() -> None:
    result = _run("profile")

    assert result.exit_code == 1
    assert "jobfeed onboard resume FILE" in result.output


def test_provider_needs_both_models() -> None:
    result = _run("provider", "claude_cli", "--quick", "quick-model")

    assert result.exit_code == USAGE_ERROR
    assert "--detailed" in result.output


def test_finish_reports_missing_steps_from_the_dashboard() -> None:
    result = _run("finish")

    assert result.exit_code == 1
    assert "/api/onboarding/finish: Confirm the job profile" in result.output


def test_shell_proxy_settings_do_not_reach_loopback_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setattr(onboard_module, "_client_factory", httpx.Client)
    with onboard_module.open_dashboard("http://127.0.0.1:7654") as client:
        assert client.trust_env is False


def test_extension_sources_wait_for_setup_and_a_connected_extension(
    tmp_path: Path, app: FastAPI
) -> None:
    early = _run("extension", "enable", "handshake")
    assert early.exit_code != 0
    assert "onboard finish" in early.output

    resume = tmp_path / "resume.md"
    resume.write_text("Platform engineer.\n", encoding="utf-8")
    _json(_run("resume", str(resume), "--no-analyze"))
    _json(_run("profile", "--confirm", "-", input_text=json.dumps(PROFILE)))
    _json(_run("searches", "--enable", "Platform Engineer"))
    _json(_run("finish"))

    offline = _run("extension", "enable", "handshake")
    assert offline.exit_code != 0
    assert "http://localhost:7654" in offline.output

    app.state.context["jobright_bridge"].connect(sources=["jobright", "handshake"])
    handshake = _json(_run("extension", "enable", "handshake"))
    assert handshake["connected"] is True  # type: ignore[index]
    assert handshake["sources"]["handshake"] == {  # type: ignore[index]
        "enabled": True,
        "queries": ["Platform Engineer"],
    }
    unsupported = _run("extension", "enable", "linkedin", "--query", "SRE")
    assert unsupported.exit_code != 0
    assert "cannot scan linkedin" in unsupported.output

    _json(_run("extension", "disable", "handshake"))
    saved = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert saved["sources"]["handshake"]["enabled"] is False
    assert saved["sources"]["handshake"]["queries"] == ["Platform Engineer"]


def _checks(result: Result) -> dict[str, bool]:
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if line[:1] == "{")
    payload = json.loads("\n".join(lines[start:]))
    return {row["check"]: row["ok"] for row in payload["checks"]}


def test_check_fails_until_setup_works_end_to_end(tmp_path: Path, app: FastAPI) -> None:
    early = _run("check")
    assert early.exit_code == 1
    assert _checks(early)["setup finished"] is False

    resume = tmp_path / "resume.md"
    resume.write_text("Platform engineer.\n", encoding="utf-8")
    _json(_run("resume", str(resume), "--no-analyze"))
    _json(_run("profile", "--confirm", "-", input_text=json.dumps(PROFILE)))
    _json(_run("searches", "--enable", "Platform Engineer"))
    _json(_run("finish"))

    ready = _run("check")
    assert ready.exit_code == 0, ready.output
    assert all(_checks(ready).values())
    assert "Chrome extension" not in _checks(ready)

    bridge = app.state.context["jobright_bridge"]
    connection = bridge.connect(sources=["jobright", "handshake"])
    _json(_run("extension", "enable", "handshake"))
    assert _run("check").exit_code == 0
    bridge.disconnect(connection)
    offline = _run("check")
    assert offline.exit_code == 1
    assert _checks(offline)["Chrome extension"] is False
