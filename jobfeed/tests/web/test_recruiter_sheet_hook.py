"""The dashboard syncs the recruiter sheet after each evaluation and reports it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from jobfeed.cli import AppContext
from jobfeed.config import Settings
from jobfeed.domain.models import PipelineRun
from jobfeed.recruiter_sheet_sync import SheetSyncError
from jobfeed.web import recruiter_sheet_hook
from tests.unit.test_run_manager import RecordingLogger

FAKE_ALERT = """\
import json, pathlib, sys
calls = pathlib.Path(__file__).resolve().parents[1] / "alert-calls.jsonl"
with calls.open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
raise SystemExit(int(__import__("os").environ.get("FAKE_ALERT_EXIT", "0")))
"""


@pytest.fixture
def pipeline_root(tmp_path: Path) -> Path:
    root = tmp_path / "resume-application-pipeline"
    (root / "pipeline").mkdir(parents=True)
    (root / "pipeline" / "__init__.py").write_text("")
    (root / "pipeline" / "recruiter_sheet_alert.py").write_text(FAKE_ALERT)
    return root


def alert_calls(root: Path) -> list[list[str]]:
    path = root / "alert-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def evaluation_run() -> PipelineRun:
    return PipelineRun(
        run_id="eval-1",
        started_at=datetime.now(UTC),
        source="evaluate",
        status="succeeded",
    )


def context_with(logger: RecordingLogger) -> AppContext:
    settings = Settings.model_validate(
        {"recruiter_sheet": {"google_sheet_id": "sheet-1"}}
    )
    return cast(AppContext, {"settings": settings, "logger": logger})


def fake_sync(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Settings],
    *,
    synced: bool = True,
    error: Exception | None = None,
) -> None:
    def sync_once(settings: Settings) -> bool:
        seen.append(settings)
        if error is not None:
            raise error
        return synced

    monkeypatch.setattr(recruiter_sheet_hook, "sync_once", sync_once)


def events(logger: RecordingLogger, name: str) -> list[dict[str, object]]:
    return [fields for event, fields in logger.events if event == name]


async def test_a_good_sync_reports_success_to_the_alert(
    monkeypatch: pytest.MonkeyPatch, pipeline_root: Path
) -> None:
    logger = RecordingLogger()
    context = context_with(logger)
    seen: list[Settings] = []
    fake_sync(monkeypatch, seen)
    hook = recruiter_sheet_hook.make_post_evaluate_hook(context, pipeline_root)

    await hook(evaluation_run())

    assert seen == [context["settings"]]
    assert alert_calls(pipeline_root) == [
        ["--root", str(pipeline_root), "--result", "success"]
    ]
    assert events(logger, "recruiter_sheet_sync_succeeded") == [{"run_id": "eval-1"}]


async def test_a_failed_sync_reports_its_reason_to_the_alert(
    monkeypatch: pytest.MonkeyPatch, pipeline_root: Path
) -> None:
    logger = RecordingLogger()
    reason = "the Jobs tab has an unexpected header, so nothing changed"
    fake_sync(monkeypatch, [], error=SheetSyncError(reason))
    hook = recruiter_sheet_hook.make_post_evaluate_hook(
        context_with(logger), pipeline_root
    )

    await hook(evaluation_run())

    assert alert_calls(pipeline_root) == [
        ["--root", str(pipeline_root), "--result", "failure", "--reason", reason]
    ]
    assert events(logger, "recruiter_sheet_sync_failed") == [
        {"run_id": "eval-1", "reason": reason}
    ]


async def test_the_sync_reads_settings_saved_after_the_dashboard_started(
    monkeypatch: pytest.MonkeyPatch, pipeline_root: Path
) -> None:
    context = context_with(RecordingLogger())
    seen: list[Settings] = []
    fake_sync(monkeypatch, seen)
    hook = recruiter_sheet_hook.make_post_evaluate_hook(context, pipeline_root)
    saved = Settings.model_validate({"recruiter_sheet": {"google_sheet_id": "new"}})
    context["settings"] = saved

    await hook(evaluation_run())

    assert seen == [saved]


async def test_no_configured_sheet_tells_the_alert_nothing(
    monkeypatch: pytest.MonkeyPatch, pipeline_root: Path
) -> None:
    fake_sync(monkeypatch, [], synced=False)
    hook = recruiter_sheet_hook.make_post_evaluate_hook(
        context_with(RecordingLogger()), pipeline_root
    )

    await hook(evaluation_run())

    assert alert_calls(pipeline_root) == []


async def test_a_missing_pipeline_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    logger = RecordingLogger()
    fake_sync(monkeypatch, [], error=SheetSyncError("boom"))
    missing = tmp_path / "nowhere"
    hook = recruiter_sheet_hook.make_post_evaluate_hook(context_with(logger), missing)

    await hook(evaluation_run())

    assert events(logger, "recruiter_sheet_alert_failed") == [
        {"run_id": "eval-1", "error": f"no application pipeline at {missing}"}
    ]


async def test_a_failing_alert_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, pipeline_root: Path
) -> None:
    logger = RecordingLogger()
    fake_sync(monkeypatch, [])
    monkeypatch.setenv("FAKE_ALERT_EXIT", "1")
    hook = recruiter_sheet_hook.make_post_evaluate_hook(
        context_with(logger), pipeline_root
    )

    await hook(evaluation_run())

    [failure] = events(logger, "recruiter_sheet_alert_failed")
    assert failure["run_id"] == "eval-1"
    assert "exited 1" in str(failure["error"])
