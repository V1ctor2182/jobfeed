"""Queue admission and explicitly authorized controller handoffs."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApplicationQueueRequest(BaseModel):
    """Request to queue canonical real jobs for preparation."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    real_job_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=100
    )


class CanonicalHandoffSelection(BaseModel):
    """Handoff selection by canonical real job IDs."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["canonical"]
    real_job_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=100
    )


class SourceHandoffSelection(BaseModel):
    """Handoff selection by source job IDs."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["source"]
    source_job_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        min_length=1, max_length=100
    )


class ApplicationHandoffRequest(BaseModel):
    """Request to hand selected jobs to an Applier runtime."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    selection: Annotated[
        CanonicalHandoffSelection | SourceHandoffSelection,
        Field(discriminator="kind"),
    ]
    authorize_dispatch: Literal[True]
    runtime: Literal["codex", "claude"] = "codex"

    @field_validator("authorize_dispatch", mode="before")
    @classmethod
    def explicit_authorization(cls, value: object) -> object:
        """Require the client to send `authorize_dispatch` as literal true.

        Args:
            value: Raw `authorize_dispatch` value before type coercion.

        Returns:
            The value unchanged.

        Raises:
            ValueError: If the value is anything other than True.
        """
        if value is not True:
            raise ValueError("Explicit dispatch authorization is required.")
        return value


class ApplicationQueueItem(BaseModel):
    """Admission outcome for one job in a queue request."""

    real_job_id: str | None
    source_job_id: int | None = None
    status: Literal["queued", "already_queued", "rejected"]
    reason: str | None = None


class ApplicationQueueReceipt(BaseModel):
    """Status of one queue or handoff request and its jobs."""

    request_id: str
    intent: Literal["queue", "handoff", "legacy_automatic"]
    status: Literal[
        "queued",
        "waiting_for_capacity",
        "starting",
        "preparing",
        "ready_for_review",
        "needs_attention",
        "unknown_delivery",
    ]
    items: list[ApplicationQueueItem]
    plan_id: str | None = None
    controller_thread_id: str | None = None
    runtime: Literal["codex", "claude"] = "codex"
    reason: str | None = None
    submit_policy: Literal["stop_before_submit"]


class ApplicationQueueConsumer(BaseModel):
    """Whether the preparation queue consumer is idle, active or blocked."""

    state: Literal["idle", "active", "blocked"]
    reason: str | None = None


class ApplicationQueueState(BaseModel):
    """Queue requests, consumer status and the CSRF token for the dashboard."""

    csrf_token: str
    requests: list[ApplicationQueueReceipt]
    consumer: ApplicationQueueConsumer


class ApplicationHandoffState(BaseModel):
    """The receipt of one handoff request, if it exists."""

    request: ApplicationQueueReceipt | None = None
