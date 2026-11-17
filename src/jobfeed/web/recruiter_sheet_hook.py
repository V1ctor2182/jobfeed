"""Bring the recruiter sheet in step after each evaluation, and report how it went.

The application pipeline owns the Discord alert, and Jobfeed never imports it, so
the outcome goes to its command. See docs/recruiter-sheet.md.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from jobfeed.cli import AppContext
from jobfeed.domain.models import PipelineRun
from jobfeed.recruiter_sheet_sync import failure_reason, sync_once
from jobfeed.services.run_manager import PostEvaluateHook

_ALERT_SECONDS = 120
_OUTPUT_LENGTH = 300


def _report(pipeline_root: Path, failure: str | None) -> str | None:
    """Pass one sync outcome to the pipeline's alert, returning why that failed."""
    if not (pipeline_root / "pipeline").is_dir():
        return f"no application pipeline at {pipeline_root}"
    outcome = ["--result", "success"]
    if failure is not None:
        outcome = ["--result", "failure", "--reason", failure]
    command = [sys.executable, "-m", "pipeline.recruiter_sheet_alert"]
    try:
        done = subprocess.run(
            [*command, "--root", str(pipeline_root), *outcome],
            cwd=pipeline_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_ALERT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode:
        output = " ".join(f"{done.stdout} {done.stderr}".split())
        return f"exited {done.returncode}: {output[-_OUTPUT_LENGTH:]}"
    return None


def make_post_evaluate_hook(
    context: AppContext, pipeline_root: Path
) -> PostEvaluateHook:
    """Build the dashboard's last step of an evaluation.

    Args:
        context: Dependency graph. Settings are read when each sync starts, so a
            saved configuration change applies to the next one.
        pipeline_root: The application pipeline that owns the Discord alert.

    Returns:
        Hook that syncs the sheet, logs the outcome and reports it to the alert.
        It never raises for a failed sync or a failed alert.
    """

    async def _post_evaluate(run: PipelineRun) -> None:
        logger = context["logger"]
        failure = None
        try:
            if not await asyncio.to_thread(sync_once, context["settings"]):
                return
        except Exception as exc:
            failure = failure_reason(exc)
            logger.error(
                "recruiter_sheet_sync_failed", run_id=run.run_id, reason=failure
            )
        else:
            logger.info("recruiter_sheet_sync_succeeded", run_id=run.run_id)
        problem = await asyncio.to_thread(_report, pipeline_root, failure)
        if problem is not None:
            logger.error(
                "recruiter_sheet_alert_failed", run_id=run.run_id, error=problem
            )

    return _post_evaluate


__all__ = ["make_post_evaluate_hook"]
