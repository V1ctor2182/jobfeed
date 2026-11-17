"""The local dashboard's one-application preparation command."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApplicationRerun(BaseModel):
    """One rerun request for a job's application and its progress."""

    request_id: str
    batch_id: str
    job_id: int
    owner_thread_id: str
    state: Literal["queued", "unknown", "running", "completed", "blocked"]
    reason: str | None = None
    created_at: str
    turn_id: str | None = None
    phase: Literal[
        "relay_pending",
        "cleanup",
        "create_unknown",
        "candidate_received",
        "adopting",
        "adopted",
        "filling",
        "completed",
        "blocked",
    ] | None = None
    replacement_thread_id: str | None = None


class ApplicationRerunState(BaseModel):
    """Whether a job can be rerun, and its latest rerun request."""

    csrf_token: str
    eligible: bool
    reason: str | None = None
    owner_thread_id: str | None = None
    request: ApplicationRerun | None = None


class ApplicationRerunRequest(BaseModel):
    """Dashboard request to rerun one job's application for its owner thread."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    batch_id: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\]+$")
    job_id: int = Field(gt=0, strict=True)
    expected_owner: str = Field(min_length=1, max_length=200)
