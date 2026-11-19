"""Compare saved onboarding choices with active local configuration."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jobfeed.config_editor import EditableConfiguration
from jobfeed.my_profile import MyProfileService
from jobfeed.my_profile_document import PersonalProfileStore
from jobfeed.onboarding_batch_setup import batch_connection_state
from jobfeed.onboarding_resume_types import ResumeDraftState
from jobfeed.onboarding_searches import SearchDraftState, searches_need_review
from jobfeed.onboarding_types import ProviderOnboardingState
from jobfeed.services.readiness_models import ReadinessRun


class ChecklistCheck(BaseModel):
    """One setup checklist item with its status and optional action link."""

    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    status: Literal["ready", "needs_action", "not_tested"]
    detail: str
    action_label: str | None = None
    action_href: str | None = None


class ChecklistSection(BaseModel):
    """A titled group of setup checklist items."""

    id: Literal["intake", "discovery", "applications"]
    title: str
    checks: list[ChecklistCheck]


class OnboardingChecklist(BaseModel):
    """The setup checklist shown on the onboarding page."""

    sections: list[ChecklistSection]


_BACKENDS = {
    "openai_api": "openai-compat",
    "anthropic_api": "anthropic-api",
    "azure_openai": "azure-openai",
    "codex_cli": "codex-cli",
    "claude_cli": "claude-cli",
    "amazon_bedrock": "bedrock",
}


def _check(key: str, label: str, ready: bool, detail: str, href: str) -> ChecklistCheck:
    return ChecklistCheck(
        id=key,
        label=label,
        status="ready" if ready else "needs_action",
        detail=detail,
        action_label="Review and save" if href == "/setup/review" else "Review setup",
        action_href=href,
    )


def database_readable(database: Path) -> bool:
    """Check that the Jobfeed database opens read-only and has a jobs table.

    Args:
        database: Path to the Jobfeed SQLite database.

    Returns:
        True when a read-only query on `jobs` succeeds.
    """
    try:
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("SELECT id FROM jobs LIMIT 1").fetchall()
        return True
    except (OSError, sqlite3.Error, ValueError):
        return False


def project_mcp_configured(root: Path, database: Path, apply_root: Path) -> bool:
    """Check that the project's `.mcp.json` registers the Jobfeed intake MCP server.

    Args:
        root: Jobfeed root; `.mcp.json` is read from its parent.
        database: Database the server must be configured to use.
        apply_root: Pipeline root the server must run from.

    Returns:
        True when the `jobfeed` server is a stdio server with the expected module,
        flags and paths.
    """
    try:
        document = json.loads((root.parent / ".mcp.json").read_text())
        server = document["mcpServers"]["jobfeed"]
        if server.get("type", "stdio") != "stdio":
            return False
        args = server["args"]
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return False
        if args[:2] != ["-m", "pipeline.jobfeed_intake_mcp"]:
            return False
        values = dict(zip(args[2::2], args[3::2], strict=True))
        if set(values) != {"--db", "--jobfeed-root", "--jobfeed-python"}:
            return False
        python = Path(server["command"])
        jobfeed_python = Path(values["--jobfeed-python"])
        return (
            python.is_absolute()
            and python.is_file()
            and os.access(python, os.X_OK)
            and jobfeed_python.is_file()
            and os.access(jobfeed_python, os.X_OK)
            and Path(values["--db"]).resolve() == database.resolve()
            and Path(values["--jobfeed-root"]).resolve() == root.resolve()
            and str(apply_root.resolve())
            in server["env"]["PYTHONPATH"].split(os.pathsep)
            and (apply_root / "pipeline/jobfeed_intake_mcp.py").is_file()
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def discovery_checks(
    root: Path,
    active: EditableConfiguration,
    provider: ProviderOnboardingState,
    resume: ResumeDraftState,
    searches: SearchDraftState,
) -> list[ChecklistCheck]:
    """Build the checks for the discovery section of the setup checklist.

    Args:
        root: Jobfeed root used to resolve relative résumé paths.
        active: Configuration currently in effect.
        provider: Model provider onboarding state.
        resume: Master résumé draft state.
        searches: Saved search draft state.

    Returns:
        Checks for the provider, models, master résumé and searches.
    """
    backend = _BACKENDS.get(provider.provider or "")
    provider_draft_ready = bool(
        provider.connected
        and backend
        and provider.quick_model
        and provider.detailed_model
    )
    models_applied = bool(
        provider_draft_ready
        and active.llm.stage_a == f"{backend}/{provider.quick_model}"
        and active.llm.stage_b == f"{backend}/{provider.detailed_model}"
    )
    selected_resume = Path(active.llm.master_resume_path).expanduser()
    if not selected_resume.is_absolute():
        selected_resume = root / selected_resume
    resume_draft_ready = bool(
        resume.is_confirmed
        and resume.profile is not None
        and resume.stored_name
        and (root / "data/resumes" / resume.stored_name).is_file()
    )
    resume_applied = bool(
        resume_draft_ready
        and resume.stored_name
        and selected_resume.resolve()
        == (root / "data/resumes" / resume.stored_name).resolve()
        and selected_resume.is_file()
        and selected_resume.stat().st_size > 0
        and "example" not in selected_resume.name.lower()
    )
    enabled = [item for item in searches.searches if item.enabled]
    preferences_changed = bool(
        resume.profile and searches_need_review(resume.profile, searches)
    )
    searches_applied = (
        bool(enabled)
        and not preferences_changed
        and all(
            getattr(active.sources, source).enabled == bool(urls)
            and list(getattr(active.sources, source).search_urls) == urls
            for source in ("linkedin_guest", "indeed")
            for urls in [[item.url for item in enabled if item.source == source]]
        )
    )
    return [
        _check(
            "provider",
            "Provider selections applied",
            models_applied,
            "Active models match the saved provider connection check and "
            "model choices. This does not retest authentication."
            if models_applied
            else (
                "Your provider and model choices are saved. "
                "Review and save to apply them."
            )
            if provider_draft_ready
            else (
                "Connect a provider, select both models, then save the setup to "
                "apply them."
            ),
            "/setup/review"
            if provider_draft_ready and not models_applied
            else "/setup/provider",
        ),
        _check(
            "resume",
            "Confirmed resume applied to scoring",
            resume_applied,
            "Scoring uses the uploaded resume associated with your confirmed "
            "job profile."
            if resume_applied
            else (
                "Your confirmed resume and profile are saved. "
                "Review and save to apply them."
            )
            if resume_draft_ready
            else (
                "Upload and confirm your resume and job profile, then save the "
                "setup. A draft alone does not change scoring."
            ),
            "/profile#resume",
        ),
        _check(
            "searches",
            "Selected searches applied",
            searches_applied,
            "Active discovery searches match your enabled saved selections."
            if searches_applied
            else "Review your existing searches against your saved job preferences."
            if preferences_changed
            else "Your enabled searches are saved. Review and save to apply them."
            if enabled
            else (
                "Enable your job searches, then save the setup to apply the selections."
            ),
            "/setup/searches"
            if preferences_changed
            else "/setup/review"
            if enabled and not searches_applied
            else "/setup/searches",
        ),
    ]


def application_checks(run: ReadinessRun | None) -> list[ChecklistCheck]:
    """Turn the latest readiness run into application checklist items.

    Args:
        run: Latest readiness run, or None when none was recorded.

    Returns:
        Checks for résumé PDFs, the relay and Chrome binding, and the overall
        readiness diagnostic; stale results are reported as not tested.
    """
    latest = {item.id: item for item in run.checks} if run else {}
    checks = []
    for key, label in (
        ("resumes", "Existing resume PDFs"),
        ("registration", "Diagnostic relay and Chrome binding"),
    ):
        evidence = latest.get(key)
        status: Literal["ready", "needs_action", "not_tested"] = "not_tested"
        detail = "Run the safe readiness check to verify this prerequisite."
        if evidence is not None:
            if run is not None and run.stale:
                detail = (
                    "The saved readiness result has expired. "
                    f"Last result: {evidence.reason} Run the safe check again."
                )
            else:
                readiness: dict[str, Literal["ready", "needs_action", "not_tested"]] = {
                    "ready": "ready",
                    "blocked": "needs_action",
                    "not_tested": "not_tested",
                }
                status = readiness[evidence.status]
                detail = evidence.reason
        checks.append(
            ChecklistCheck(
                id=key,
                label=label,
                status=status,
                detail=detail,
                action_label="Manage résumés"
                if key == "resumes"
                else "Check readiness",
                action_href="/profile#resume"
                if key == "resumes"
                else "/setup#application-readiness",
            )
        )
    ready = bool(run and not run.stale and run.ready and run.phase == "completed")
    checks.append(
        ChecklistCheck(
            id="readiness",
            label="Application readiness diagnostic",
            status="ready"
            if ready
            else "needs_action"
            if run and not run.stale and run.phase == "blocked"
            else "not_tested",
            detail=(
                "The latest safe diagnostic passed and is still current. "
                "Starting applications requires your separate instruction."
            )
            if ready
            else (
                "Run the safe readiness check. It verifies local files, Codex "
                "Desktop and Chrome without starting applications."
            ),
            action_label="Check readiness",
            action_href="/setup#application-readiness",
        )
    )
    return checks


@dataclass(frozen=True, kw_only=True)
class ChecklistInputs:
    """Everything the setup checklist reads to decide each item's status."""

    root: Path
    database: Path
    apply_root: Path
    active: EditableConfiguration
    provider: ProviderOnboardingState
    resume: ResumeDraftState
    searches: SearchDraftState
    readiness: ReadinessRun | None
    dashboard_origin: str | None = None


def _personal_check(inputs: ChecklistInputs) -> ChecklistCheck:
    try:
        state = PersonalProfileStore(
            inputs.apply_root / "application-profile.md",
            inputs.root / "data/my-profile-review.json",
        ).state()
        missing = [
            label
            for key, label in (
                ("legal_name", "name"),
                ("primary_email", "email"),
                ("phone", "phone"),
            )
            if not getattr(state.personal_details, key)
        ]
        ready = state.reviewed and not missing
        detail = (
            "Your personal details have been reviewed. "
            "Blank optional fields remain unknown."
            if ready
            else "Add your " + ", ".join(missing) + " and save your personal details."
            if missing
            else "Review and save your existing personal details in My Profile."
        )
    except (OSError, ValueError, TypeError):
        ready = False
        detail = "Open My Profile to review or repair your personal details."
    return ChecklistCheck(
        id="profile",
        label="Personal details reviewed",
        status="ready" if ready else "needs_action",
        detail=detail,
        action_label="Edit personal details",
        action_href="/profile#personal-details",
    )


def _catalog_check(inputs: ChecklistInputs) -> ChecklistCheck:
    try:
        ready = MyProfileService(
            root=inputs.root,
            pipeline_root=inputs.apply_root,
            resume_state=lambda: inputs.resume,
        ).catalog_ready()
    except (OSError, ValueError, TypeError):
        ready = False
    return ChecklistCheck(
        id="resumes",
        label="Application résumé PDFs selected",
        status="ready" if ready else "needs_action",
        detail="Your selected PDFs are available for future approved batches."
        if ready
        else "Add or enable an existing application PDF in My Profile.",
        action_label="Manage résumés",
        action_href="/profile#resume",
    )


def build_checklist(inputs: ChecklistInputs) -> OnboardingChecklist:
    """Build the full setup checklist from local state.

    Args:
        inputs: Configuration, onboarding drafts and readiness results to read.

    Returns:
        The intake, discovery and application sections.
    """
    database_ok = database_readable(inputs.database)
    mcp_ok = project_mcp_configured(inputs.root, inputs.database, inputs.apply_root)
    batch = batch_connection_state(
        inputs.apply_root,
        inputs.database,
        inputs.dashboard_origin,
        project_root=inputs.root.parent,
    )
    application = [
        _personal_check(inputs),
        _catalog_check(inputs),
        *(
            check
            for check in application_checks(inputs.readiness)
            if check.id != "resumes"
        ),
    ]
    application.append(
        _check(
            "batch_setup",
            "Claude batch connection configured",
            batch.configured,
            batch.detail,
            "/setup#application-readiness",
        )
    )
    return OnboardingChecklist(
        sections=[
            ChecklistSection(
                id="intake",
                title="Add job URLs",
                checks=[
                    ChecklistCheck(
                        id="database",
                        label="Job database readable",
                        status="ready" if database_ok else "needs_action",
                        detail="The dashboard can read the configured job database. "
                        "An empty queue is valid."
                        if database_ok
                        else (
                            "The configured job database could not be read. "
                            "Ask Codex to repair the local database configuration."
                        ),
                    ),
                    ChecklistCheck(
                        id="mcp",
                        label="Project MCP configured",
                        status="ready" if mcp_ok else "needs_action",
                        detail="The project Jobfeed MCP points to this database and "
                        "combined workspace. Load the connection in your local "
                        "agent client."
                        if mcp_ok
                        else (
                            "Ask Codex to configure the project Jobfeed MCP for "
                            "this workspace, Python and database."
                        ),
                    ),
                    ChecklistCheck(
                        id="mcp_client",
                        label="Codex or Claude connection",
                        status="not_tested",
                        detail=(
                            "In local Codex or Claude Code, load the project Jobfeed "
                            "MCP and call capabilities. Use add_job_urls for up to 20 "
                            "URLs per batch. This dashboard cannot observe your "
                            "agent connection. Importing URLs does not require "
                            "scoring setup or start applications."
                        ),
                    ),
                ],
            ),
            ChecklistSection(
                id="discovery",
                title="Discover and score jobs",
                checks=discovery_checks(
                    inputs.root,
                    inputs.active,
                    inputs.provider,
                    inputs.resume,
                    inputs.searches,
                ),
            ),
            ChecklistSection(
                id="applications",
                title="Prepare applications",
                checks=application,
            ),
        ]
    )
