"""Adapter to the pipeline's record of application email and application stages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

MailStatus = Literal["applied", "action", "update", "interviewing", "rejected", "offer"]
Stage = Literal["applied", "ghosted", "interviewing", "rejected", "offer"]


class TrackerMessage(BaseModel):
    """One application email: its sender, subject, snippet, and what it reports."""

    id: str
    thread_id: str
    sender: str
    sender_name: str | None = None
    subject: str
    snippet: str
    received_at: str
    view_url: str | None = None
    status: MailStatus
    company: str | None = None
    job_ids: list[int] = Field(default_factory=list)


class TrackerApplication(BaseModel):
    """One application and the stage its email puts it in."""

    job_id: int | None = None
    company: str | None = None
    title: str | None = None
    url: str | None = None
    applied_at: str | None = None
    source: Literal["jobfeed", "email"]
    batch_id: str | None = None
    stage: Stage
    confirmed_by_email: bool
    email_count: int
    last_activity_at: str | None = None


class TrackerSync(BaseModel):
    """The latest mail sync: when it finished, how much was new, and any error."""

    finished_at: str
    new_messages: int
    error: str | None = None


class TrackerState(BaseModel):
    """The application inbox and every application's stage."""

    messages: list[TrackerMessage]
    applications: list[TrackerApplication]
    last_sync: TrackerSync | None = None


class TrackerBody(BaseModel):
    """One application email with its plain-text body."""

    id: str
    sender: str
    sender_name: str | None = None
    subject: str
    received_at: str
    view_url: str | None = None
    status: str
    company: str | None = None
    body: str
    recipients: list[str] = Field(default_factory=list)
    fetched_at: str


class TrackerError(Exception):
    """The tracker could not be read."""


class TrackerNotFound(TrackerError):
    """The message is not one the tracker saved, or Gmail no longer has it."""


class TrackerService:
    """Reads the tracker through the pipeline's CLI. It never syncs mail itself."""

    def __init__(self, root: Path, jobfeed_db: Path) -> None:
        self.root, self.jobfeed_db = root.resolve(), jobfeed_db.resolve()

    def _run(self, *command: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.root / ".venv/bin/python"),
                "-m",
                "pipeline.mail_tracker",
                "--root",
                str(self.root),
                "--jobfeed-db",
                str(self.jobfeed_db),
                *command,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def state(self) -> TrackerState:
        """Read the saved application email and each application's stage.

        Returns:
            The tracker state reported by the pipeline.

        Raises:
            TrackerError: If the pipeline cannot report it.
        """
        try:
            result = self._run("view", timeout=20)
            if result.returncode:
                raise ValueError("view failed")
            return TrackerState.model_validate(json.loads(result.stdout))
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            TypeError,
            ValidationError,
        ):
            raise TrackerError("Application email could not be read.") from None

    def body(self, message_id: str) -> TrackerBody:
        """Read one saved email with its body, fetching the body from Gmail once.

        Args:
            message_id: The saved message to open.

        Returns:
            The message and its plain-text body.

        Raises:
            TrackerNotFound: If the tracker never saved this message.
            TrackerError: If the body cannot be fetched.
        """
        try:
            result = self._run("body", "--id", message_id, timeout=200)
            payload = json.loads(result.stdout)
            if result.returncode:
                raise TrackerNotFound(str(payload.get("error", "unknown message")))
            return TrackerBody.model_validate(payload)
        except TrackerNotFound:
            raise
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            TypeError,
            AttributeError,
            ValidationError,
        ):
            raise TrackerError("This email could not be fetched.") from None
