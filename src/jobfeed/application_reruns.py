"""Delegate dashboard preparation requests to the owning application pipeline."""

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

from jobfeed.services.application_rerun_models import (
    ApplicationRerunRequest,
    ApplicationRerunState,
)

_RESPONSE_LIMIT = 32768


class ApplicationRerunError(Exception):
    """A rerun command failure with an API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApplicationRerunService:
    """Reads and requests application reruns through the pipeline's CLI."""

    def __init__(self, root: Path, jobfeed_db: Path) -> None:
        self.root, self.jobfeed_db = root.resolve(), jobfeed_db.resolve()
        self.csrf_token = secrets.token_urlsafe(32)

    def _command(self, command: str, payload: dict[str, Any]) -> ApplicationRerunState:
        try:
            result = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "pipeline.application_rerun",
                    "--root",
                    str(self.root),
                    "--jobfeed-db",
                    str(self.jobfeed_db),
                    command,
                ],
                input=json.dumps(payload),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=85,
                check=False,
            )
            if len(result.stdout) > _RESPONSE_LIMIT:
                raise ValueError("Oversized rerun response")
            body = json.loads(result.stdout)
            if result.returncode:
                code = body.get("code", "unavailable")
                raise ApplicationRerunError(
                    code
                    if code in {"invalid", "conflict", "unavailable"}
                    else "unavailable",
                    body.get("error", "Application rerun is unavailable."),
                )
            return ApplicationRerunState.model_validate(
                {**body, "csrf_token": self.csrf_token}
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise ApplicationRerunError(
                "unavailable",
                "Could not verify the rerun result. "
                "Refresh its status; do not create a new request.",
            ) from None

    def state(self, batch_id: str, job_id: int) -> ApplicationRerunState:
        """Read the rerun state of one job in a batch.

        Args:
            batch_id: Batch the job belongs to.
            job_id: Job whose rerun state should be read.

        Returns:
            The rerun state reported by the pipeline.
        """
        return self._command("status", {"batch_id": batch_id, "job_id": job_id})

    def request(self, body: ApplicationRerunRequest, dashboard_url: str) -> ApplicationRerunState:
        """Ask the pipeline to rerun a job's application.

        Args:
            body: The rerun request from the dashboard.
            dashboard_url: Dashboard URL passed to the pipeline with the request.

        Returns:
            The rerun state after the request.
        """
        return self._command("dashboard-request", {**body.model_dump(mode="json"), "dashboard_url": dashboard_url})
