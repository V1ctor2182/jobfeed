"""Setup reports active configuration and bounded readiness evidence."""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.config import Settings
from jobfeed.config_editor import EditableConfiguration
from jobfeed.my_profile_document import PersonalProfileStore
from jobfeed.my_profile_models import PersonalDetails
from jobfeed.onboarding_checklist import (
    ChecklistInputs,
    application_checks,
    build_checklist,
)
from jobfeed.onboarding_resume_types import JobProfile, ResumeDraftState
from jobfeed.onboarding_searches import SearchDraftState, SearchSuggestion
from jobfeed.onboarding_types import ProviderOnboardingState
from jobfeed.services.readiness_models import ReadinessCheck, ReadinessRun
from jobfeed.web.routes.onboarding_checklist import router

HTTP_OK = 200


def _inputs(tmp_path: Path) -> ChecklistInputs:
    root = tmp_path / "jobfeed"
    root.mkdir()
    apply_root = tmp_path / "resume-application-pipeline"
    (apply_root / "pipeline").mkdir(parents=True)
    (apply_root / "pipeline/jobfeed_intake_mcp.py").write_text("")
    database = root / "jobs.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jobfeed": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "pipeline.jobfeed_intake_mcp",
                            "--db",
                            str(database),
                            "--jobfeed-root",
                            str(root),
                            "--jobfeed-python",
                            sys.executable,
                        ],
                        "env": {
                            "PYTHONPATH": str(apply_root),
                            "PRIVATE_TOKEN": "must-not-return",
                        },
                    }
                },
            }
        )
    )
    return ChecklistInputs(
        root=root,
        database=database,
        apply_root=apply_root,
        active=EditableConfiguration(),
        provider=ProviderOnboardingState(),
        resume=ResumeDraftState(),
        searches=SearchDraftState(),
        readiness=None,
    )


def _by_id(snapshot):
    return {
        check.id: check for section in snapshot.sections for check in section.checks
    }


def _profile() -> JobProfile:
    return JobProfile(
        desired_titles=["Engineer"],
        seniority_levels=[],
        target_countries=[],
        target_locations=[],
        work_modes=[],
        industries=[],
        company_sizes=[],
        work_authorization="",
        hiring_timeline="",
        excluded_titles=[],
        excluded_companies=[],
        excluded_locations=[],
        excluded_keywords=[],
        resume_evidence=[],
    )


def test_url_intake_is_independent_of_unfinished_scoring(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    (inputs.root / "config.toml").write_text("[llm]\n")
    before = inputs.database.read_bytes()
    snapshot = build_checklist(inputs)
    checks = _by_id(snapshot)
    assert checks["database"].status == "ready"
    assert checks["mcp"].status == "ready"
    assert checks["mcp_client"].status == "not_tested"
    assert checks["provider"].status == "needs_action"
    assert checks["resume"].status == "needs_action"
    assert checks["searches"].status == "needs_action"
    assert checks["readiness"].status == "not_tested"
    assert checks["batch_setup"].status == "needs_action"
    assert "coordination" not in checks
    assert checks["readiness"].action_href == "/setup#application-readiness"
    assert "must-not-return" not in snapshot.model_dump_json()
    assert str(tmp_path) not in snapshot.model_dump_json()
    assert inputs.database.read_bytes() == before


def test_project_mcp_must_match_actual_database(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    path = tmp_path / ".mcp.json"
    config = json.loads(path.read_text())
    config["mcpServers"]["jobfeed"]["args"][3] = str(tmp_path / "other.sqlite")
    path.write_text(json.dumps(config))
    checks = _by_id(build_checklist(inputs))
    assert checks["mcp"].status == "needs_action"
    inputs.database.unlink()
    assert _by_id(build_checklist(inputs))["database"].status == "needs_action"
    assert not inputs.database.exists()


def test_applied_choices_must_match_saved_drafts(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    resume_file = inputs.root / "data/resumes/uploaded.md"
    resume_file.parent.mkdir(parents=True)
    resume_file.write_text("Actual resume text")
    provider = ProviderOnboardingState(
        provider="codex_cli",
        connected=True,
        quick_model="quick",
        detailed_model="deep",
    )
    resume = ResumeDraftState(
        stored_name="uploaded.md",
        is_confirmed=True,
        profile=_profile(),
    )
    search = SearchSuggestion(
        id="search-1",
        source="indeed",
        query="Engineer",
        location="US",
        url="https://www.indeed.com/jobs?q=Engineer",
        enabled=True,
    )
    inputs = replace(
        inputs,
        provider=provider,
        resume=resume,
        searches=SearchDraftState(searches=[search], profile_snapshot=_profile()),
    )
    draft_checks = _by_id(build_checklist(inputs))
    for key in ("provider", "resume", "searches"):
        assert draft_checks[key].status == "needs_action"
        expected = "/profile#resume" if key == "resume" else "/setup/review"
        assert draft_checks[key].action_href == expected
    payload = inputs.active.model_dump()
    payload["llm"].update(
        stage_a="codex-cli/quick",
        stage_b="codex-cli/deep",
        master_resume_path="data/resumes/uploaded.md",
    )
    payload["sources"]["indeed"].update(enabled=True, search_urls=[search.url])
    payload["sources"]["linkedin_guest"].update(enabled=False, search_urls=[])
    inputs = replace(inputs, active=EditableConfiguration.model_validate(payload))
    checks = _by_id(build_checklist(inputs))
    assert checks["provider"].status == "ready"
    assert checks["resume"].status == "ready"
    assert checks["searches"].status == "ready"
    resume_file.unlink()
    assert _by_id(build_checklist(inputs))["resume"].status == "needs_action"


def test_expired_diagnostic_does_not_mark_materials_or_application_ready() -> None:
    run = ReadinessRun(
        run_id="saved-run",
        phase="completed",
        started_at="2026-10-21T12:00:00Z",
        finished_at="2026-10-21T12:01:00Z",
        expires_at="2026-10-21T12:06:00Z",
        stale=False,
        ready=True,
        checks=[
            ReadinessCheck(
                id="resumes",
                label="Resume PDFs",
                status="ready",
                required=True,
                checked_at="2026-10-21T12:01:00Z",
                reason="Existing PDF validated.",
            )
        ],
    )
    checks = {item.id: item for item in application_checks(run)}
    assert checks["resumes"].status == "ready"
    assert checks["readiness"].status == "ready"
    checks = {
        item.id: item
        for item in application_checks(run.model_copy(update={"stale": True}))
    }
    assert checks["resumes"].status == "not_tested"
    assert checks["readiness"].status == "not_tested"
    assert "expired" in checks["resumes"].detail
    assert "Existing PDF validated." in checks["resumes"].detail


@pytest.mark.asyncio
async def test_endpoint_reads_without_generating_drafts_or_dispatching(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.onboarding_root = inputs.root
    app.state.context = {"settings": Settings(db={"path": inputs.database})}
    app.state.apply_health_reader = SimpleNamespace(root=inputs.apply_root)
    app.state.configuration_editor = SimpleNamespace(editable=inputs.active)
    app.state.onboarding_provider_service = SimpleNamespace(
        state=lambda: inputs.provider
    )
    app.state.onboarding_resume_service = SimpleNamespace(state=lambda: inputs.resume)
    app.state.apply_readiness = SimpleNamespace(get=lambda: None)
    before = set(tmp_path.rglob("*"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/onboarding/checklist")
    assert response.status_code == HTTP_OK
    assert [item["id"] for item in response.json()["sections"]] == [
        "intake",
        "discovery",
        "applications",
    ]
    assert set(tmp_path.rglob("*")) == before
    assert "must-not-return" not in response.text


@pytest.mark.parametrize("value", ["{broken", "[]", '{"mcpServers":null}'])
def test_malformed_mcp_is_actionable_without_leaking_config(
    tmp_path: Path, value: str
) -> None:
    inputs = _inputs(tmp_path)
    (tmp_path / ".mcp.json").write_text(value)
    snapshot = build_checklist(inputs)
    assert _by_id(snapshot)["mcp"].status == "needs_action"
    assert str(tmp_path) not in snapshot.model_dump_json()


def test_personal_check_requires_review_not_just_nonempty_file(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    profile = inputs.apply_root / "application-profile.md"
    profile.write_text("# Application profile\n")
    assert _by_id(build_checklist(inputs))["profile"].status == "needs_action"
    store = PersonalProfileStore(profile, inputs.root / "data/my-profile-review.json")
    state = store.state()
    store.save(
        PersonalDetails(
            legal_name="Test", primary_email="test@example.test", phone="555-0100"
        ),
        state.revision,
    )
    check = _by_id(build_checklist(inputs))["profile"]
    assert check.status == "ready"
    assert check.action_href == "/profile#personal-details"


def test_disabling_all_catalog_pdfs_requires_action(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    pdf = inputs.apply_root / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    catalog = inputs.apply_root / "resume-catalog.json"
    content = {
        "schema_version": 1,
        "resumes": [
            {"id": "resume", "label": "Resume", "pdf": "resume.pdf", "available": True}
        ],
    }
    catalog.write_text(json.dumps(content))
    assert _by_id(build_checklist(inputs))["resumes"].status == "ready"
    content["resumes"][0]["available"] = False
    catalog.write_text(json.dumps(content))
    assert _by_id(build_checklist(inputs))["resumes"].status == "needs_action"
