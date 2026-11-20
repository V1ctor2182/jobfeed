"""Adapter to the pipeline's record of application email and application stages."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from jobfeed.applications_runtime import ApplicationsNotInstalled

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


class GmailCheck(BaseModel):
    """Whether one small read-only Gmail search worked, and what to do if not."""

    connected: bool
    recent_threads: int | None = None
    reason: str | None = None
    fix: str | None = None


_CONNECT_GMAIL = (
    "On claude.ai, open Settings > Connectors and connect Gmail, using the same "
    "Claude account this machine is signed in to. Then run `jobfeed email check`."
)


def gmail_problem(error: str) -> tuple[str, str]:
    """Explain a Gmail connector failure in plain words, with its fix.

    Args:
        error: The pipeline's error text.

    Returns:
        What went wrong, and how to fix it.
    """
    if "did not run" in error:
        return (
            "Claude Code is not installed or not on this program's PATH.",
            "Install Claude Code, sign in with your Claude account, and try again.",
        )
    if "no result" in error:
        return (
            "Gmail did not answer: the Claude account signed in on this machine has "
            "no Gmail connector, or its sign-in expired.",
            _CONNECT_GMAIL,
        )
    if "not JSON" in error:
        return (
            "Gmail answered with something unexpected.",
            "Run `jobfeed email check` again; if it repeats, reconnect Gmail. "
            + _CONNECT_GMAIL,
        )
    return (
        f"the email check stopped on an error ({error}).",
        "Run `jobfeed email sync` to try again. If it keeps failing, update "
        "Jobfeed, which may already have a fix.",
    )


class TrackerService:
    """Reads the tracker through the pipeline's CLI.

    The dashboard never syncs mail; the sync runs from `jobfeed email sync`.
    """

    def __init__(self, root: Path, jobfeed_db: Path) -> None:
        self.root, self.jobfeed_db = root.resolve(), jobfeed_db.resolve()

    def _run(self, *command: str, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return self._spawn(*command, timeout=timeout)
        except FileNotFoundError:
            raise ApplicationsNotInstalled from None

    def _python(self) -> str:
        # The full pipeline environment when the Applier is installed; otherwise
        # Jobfeed's own Python, which has all the mail tracker needs.
        pipeline = self.root / ".venv/bin/python"
        return str(pipeline) if pipeline.exists() else sys.executable

    def _spawn(self, *command: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self._python(),
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

    def mail_command(self, command: str, *, timeout: int) -> dict[str, object]:
        """Run one Gmail command of the mail tracker and return its JSON.

        Args:
            command: ``check`` or ``sync``.
            timeout: Seconds before giving up.

        Returns:
            The command's JSON, with ``error`` set when it failed.
        """
        try:
            result = self._run(command, timeout=timeout)
            payload = json.loads(result.stdout)
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            ApplicationsNotInstalled,
        ) as exc:
            return {"error": type(exc).__name__}
        if not isinstance(payload, dict):
            return {"error": "unexpected output"}
        if result.returncode and "error" not in payload:
            payload["error"] = result.stderr.strip()[-200:] or "failed"
        return payload

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


def check_gmail(service: TrackerService) -> GmailCheck:
    """Make one small read-only Gmail search through the connector.

    Args:
        service: Tracker adapter for this machine's pipeline folder.

    Returns:
        Whether Gmail answered, or the reason and fix.
    """
    payload = service.mail_command("check", timeout=240)
    if "error" in payload:
        reason, fix = gmail_problem(str(payload["error"]))
        return GmailCheck(connected=False, reason=reason, fix=fix)
    threads = payload.get("recent_threads")
    return GmailCheck(
        connected=True, recent_threads=threads if isinstance(threads, int) else None
    )


def sync_mail(service: TrackerService) -> dict[str, object]:
    """Save new application email and label it. Reads Gmail; sends nothing.

    Args:
        service: Tracker adapter for this machine's pipeline folder.

    Returns:
        ``new_messages`` and ``labelled``, or ``error`` with ``reason`` and ``fix``.
    """
    payload = service.mail_command("sync", timeout=900)
    if "error" in payload:
        reason, fix = gmail_problem(str(payload["error"]))
        payload.update(reason=reason, fix=fix)
    return payload
