"""Run fixed diagnostic commands in the configured pipeline source runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from jobfeed.services.readiness_models import ReadinessRun

_MAX_RESULT_BYTES = 65536


class PipelineReadiness:
    """Bind diagnostics to the dashboard's actual Python and Jobfeed database."""

    def __init__(self, root: Path, database: Path) -> None:
        """Bind explicit paths without dispatching work.

        Args:
            root: Application source and local runtime root.
            database: Actual Jobfeed database selected by settings.
        """
        self.root = root.resolve()
        self.database = database.resolve()
        self._unavailable: ReadinessRun | None = None

    def _missing_source(self, request_id: str) -> ReadinessRun:
        now = datetime.now(UTC).isoformat()
        components = {
            "profile": "Application profile",
            "resumes": "Resume catalog and PDFs",
            "python": "Python and pipeline imports",
            "execution_python": "Application worker Python",
            "node": "Node runtime",
            "autofill": "Autofill module",
            "jobfeed_cli": "Jobfeed Python CLI",
            "execution_cli": "Application Jobfeed executable",
            "jobfeed_db": "Jobfeed database schema",
            "artifacts": "Diagnostic artifact writing",
            "codex_cli": "Codex CLI",
            "desktop": "Codex Desktop",
            "browser_runtime": "Official Chrome browser runtime",
            "registration": "Diagnostic relay and browser binding",
            "harness": "Codex Desktop diagnostic execution",
            "chrome": "Chrome extension connection",
            "upload": "Synthetic PDF file chooser",
            "cleanup": "Owned diagnostic tab cleanup",
        }
        checks = [
            {
                "id": "runtime_root",
                "label": "Application runtime root",
                "status": "blocked",
                "required": True,
                "checked_at": now,
                "reason": "Configured application source is missing or incomplete.",
                "next_step": "Set JOBFEED_APPLY_ROOT to the application source "
                "checkout and restart the dashboard.",
            }
        ]
        checks.extend(
            {
                "id": key,
                "label": label,
                "status": "not_tested",
                "required": True,
                "checked_at": None,
                "reason": "Restore the configured application source "
                "before checking this component.",
                "next_step": "Restore the application source and run readiness again.",
            }
            for key, label in components.items()
        )
        return ReadinessRun.model_validate(
            {
                "run_id": request_id,
                "phase": "blocked",
                "started_at": now,
                "finished_at": now,
                "expires_at": None,
                "stale": False,
                "ready": False,
                "checks": checks,
            }
        )

    def _command(self, command: str, *arguments: str) -> object:
        env = dict(os.environ)
        jobfeed_source = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join([str(self.root), jobfeed_source])
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pipeline.application_readiness",
                    "--root",
                    str(self.root),
                    "--database",
                    str(self.database),
                    command,
                    *arguments,
                ],
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=50,
                check=False,
            )
            if result.returncode or len(result.stdout) > _MAX_RESULT_BYTES:
                raise RuntimeError("configured_pipeline_unavailable")
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise RuntimeError("configured_pipeline_unavailable") from None

    def start(self, request_id: str, origin: str) -> ReadinessRun:
        """Start or reuse the configured harmless diagnostic.

        Args:
            request_id: Validated idempotency UUID.
            origin: Validated loopback origin.

        Returns:
            Redacted diagnostic status.
        """
        if (
            not (self.root / "pipeline/application_readiness.py").is_file()
            or not (self.root / "pipeline/cli.py").is_file()
        ):
            self._unavailable = self._missing_source(request_id)
            return self._unavailable
        self._unavailable = None
        return ReadinessRun.model_validate(
            self._command("start", "--request-id", request_id, "--origin", origin)
        )

    def get(self) -> ReadinessRun | None:
        """Reconcile the latest diagnostic without sending work.

        Returns:
            Latest redacted status, or None before the first run.
        """
        if not (self.root / ".pipeline/application-readiness.sqlite3").is_file():
            return self._unavailable
        value = self._command("get")
        return ReadinessRun.model_validate(value) if value else None

    def fixture(self, run_id: str) -> str:
        """Return the static local fixture for an admitted diagnostic.

        Args:
            run_id: Admitted diagnostic UUID.

        Returns:
            Synthetic fixture HTML.

        Raises:
            ValueError: If the fixed command returned an invalid fixture.
        """
        value = self._command("fixture", "--run-id", run_id)
        if not isinstance(value, str):
            raise ValueError("invalid_fixture")
        return value
