"""Adapter to the pipeline's private, versioned answer store."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobfeed.applications_runtime import ApplicationsNotInstalled


class AnswerScope(BaseModel):
    """Where an answer applies: everywhere, one company, or one job."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["global", "company", "job"]
    company: str | None = None
    job_id: str | None = None


class AnswerDraft(BaseModel):
    """A question and answer the user is writing or editing."""

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=10000)
    answer: str = Field(min_length=1, max_length=50000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    scope: AnswerScope
    valid_until: str | None = None


class AnswerVersion(AnswerDraft):
    """One stored version of an answer and its review state."""

    model_config = ConfigDict(extra="ignore")
    version: int
    state: Literal["pending_review", "approved", "revoked", "superseded"]
    created_at: str
    approval: dict[str, Any] | None = None
    imported_status: str | None = None
    review_decision: dict[str, Any] | None = None
    superseded_state: str | None = None
    superseded_at: str | None = None
    revoked_at: str | None = None
    merged_into: str | None = None


class AnswerEntry(BaseModel):
    """An answer bank entry with every version and the current one."""

    id: str
    current_version: int
    versions: list[AnswerVersion]


class CommonQuestion(BaseModel):
    """A question most applications ask, with the saved global answer for it."""

    key: str
    group: Literal["demographic", "application"]
    question: str
    aliases: list[str]
    options: list[str]
    entry_id: str | None = None


class AnswerBankState(BaseModel):
    """The whole answer bank at one revision."""

    schema_version: Literal[1]
    revision: int
    entries: list[AnswerEntry]
    common_questions: list[CommonQuestion] = Field(default_factory=list)


class AnswerBankMutation(BaseModel):
    """A dashboard change to the answer bank, checked against a revision."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "edit", "approve", "revoke", "save", "merge"]
    expected_revision: int = Field(ge=0)
    entry_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    draft: AnswerDraft | None = None
    merge_entry_ids: list[str] | None = Field(
        default=None, min_length=1, max_length=100
    )


class AnswerBankError(Exception):
    """An answer bank command failure with an API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AnswerBankService:
    """Reads and changes the answer bank through the pipeline's CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _command(self, command: str, payload: dict[str, Any]) -> AnswerBankState:
        try:
            result = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "pipeline.answer_bank",
                    "--root",
                    str(self.root),
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
                raise ValueError("Invalid answer bank response")
            if result.returncode:
                code = body.get("code", "unavailable")
                if code not in {"conflict", "invalid", "unavailable"}:
                    code = "unavailable"
                raise AnswerBankError(
                    code, body.get("error", "Answer bank is unavailable.")
                )
            return AnswerBankState.model_validate(body)
        except FileNotFoundError:
            raise ApplicationsNotInstalled from None
        except (
            OSError,
            subprocess.TimeoutExpired,
            ValueError,
            TypeError,
            ValidationError,
        ):
            raise AnswerBankError(
                "unavailable",
                "The application answer bank could not be read. "
                "Check the application runtime and try again.",
            ) from None

    def state(self) -> AnswerBankState:
        """Read the current answer bank.

        Returns:
            The answer bank state reported by the pipeline.
        """
        return self._command("list", {})

    def mutate(self, mutation: AnswerBankMutation) -> AnswerBankState:
        """Apply one dashboard change to the answer bank.

        Args:
            mutation: The change and the revision it expects.

        Returns:
            The answer bank state after the change.
        """
        return self._command("dashboard-mutate", mutation.model_dump(exclude_none=True))
