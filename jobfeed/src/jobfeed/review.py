"""Adapter to the pipeline's open Applier questions and the user's replies to them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobfeed.applications_runtime import ApplicationsNotInstalled


class ReviewItem(BaseModel):
    """One question or blocker an Applier is waiting on the user for."""

    id: str
    job_id: int
    kind: Literal["question", "blocker"]
    blocker_type: str | None = None
    company: str | None = None
    title: str | None = None
    posting_url: str | None = None
    page_url: str | None = None
    field: str | None = None
    text: str
    draft: str | None = None
    options: list[str] = Field(default_factory=list)
    quick_replies: list[str] = Field(default_factory=list)
    created_at: str


class ReviewSite(BaseModel):
    """A job board that stopped several Appliers at a sign-in only the user can do."""

    site: str
    label: str
    held: bool
    request_ids: list[str] = Field(default_factory=list)
    job_ids: list[int] = Field(default_factory=list)
    queued: int = 0
    page_url: str | None = None
    latest_stop_at: str | None = None


class ReviewState(BaseModel):
    """Open questions and blockers that a running or parked Applier waits on."""

    items: list[ReviewItem]
    sites: list[ReviewSite] = Field(default_factory=list)
    ended: int = Field(
        default=0, description="Open requests hidden because their Applier has ended."
    )


class ReviewReply(BaseModel):
    """The user's reply to one open request, made by one click in the dashboard."""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    action: Literal["pick", "approve", "quick", "answer", "change_request"]
    text: str | None = Field(default=None, max_length=10000)
    click_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ReviewIgnore(BaseModel):
    """The user's decision to stop one job, made by one click in the dashboard."""

    model_config = ConfigDict(extra="forbid")
    job_id: int = Field(gt=0)
    click_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ReviewResume(BaseModel):
    """The user's statement that they signed in to a job board, made by one click."""

    model_config = ConfigDict(extra="forbid")
    site: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    click_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ReviewError(Exception):
    """A review command failure with an API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReviewService:
    """Reads open requests and saves the user's replies through the pipeline's CLI."""

    def __init__(self, root: Path, jobfeed_db: Path) -> None:
        self.root, self.jobfeed_db = root.resolve(), jobfeed_db.resolve()

    def _command(self, command: str, payload: dict[str, Any]) -> ReviewState:
        try:
            result = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "pipeline.dashboard_review",
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
                timeout=15,
                check=False,
            )
            body = json.loads(result.stdout)
            if not isinstance(body, dict):
                raise ValueError("Invalid review response")
        except FileNotFoundError:
            raise ApplicationsNotInstalled from None
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise ReviewError(
                "unavailable",
                "Open questions could not be read. "
                "Check the application runtime and try again.",
            ) from None
        if result.returncode:
            raise ReviewError(
                "invalid", str(body.get("error", "The reply was not saved."))
            )
        try:
            return ReviewState.model_validate(body)
        except ValidationError:
            raise ReviewError(
                "unavailable", "Open questions could not be read."
            ) from None

    def state(self) -> ReviewState:
        """Read every open question and blocker.

        Returns:
            The open requests reported by the pipeline.
        """
        return self._command("list", {})

    def reply(self, reply: ReviewReply) -> ReviewState:
        """Save the user's reply to one open request.

        Args:
            reply: The request, the kind of reply and its text.

        Returns:
            The open requests after the reply.
        """
        return self._command("reply", reply.model_dump(exclude_none=True))

    def ignore(self, ignore: ReviewIgnore) -> ReviewState:
        """Record the user's decision to stop one job.

        Args:
            ignore: The job to stop.

        Returns:
            The open requests after the job was stopped.
        """
        return self._command("ignore", ignore.model_dump())

    def resume_site(self, resume: ReviewResume) -> ReviewState:
        """Let a held job board's jobs start again and wake the Appliers it stopped.

        Args:
            resume: The job board the user signed in to.

        Returns:
            The open requests after the board was resumed.
        """
        return self._command("resume-site", resume.model_dump())
