"""Public, allowlisted observations for the combined Apply dashboard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Availability = Literal["available", "missing", "unavailable"]


class HealthModel(BaseModel):
    """Validate public observation fields while excluding unknown input fields."""

    model_config = ConfigDict(strict=True, extra="ignore")


class RuntimeHealth(HealthModel):
    """Availability and configured origin of the application runtime directory."""

    state: Availability
    source: Literal["combined_sibling", "explicit_override"]
    error: str | None = None


class ConfirmationResponse(HealthModel):
    """Dashboard summary of a job's submission confirmation evidence."""

    job_id: int
    status: Literal["available", "unverified"]
    reason: str | None = None
    kind: (
        Literal["website_screenshot", "confirmation_email", "approved_submit_email"]
        | None
    ) = None
    captured_at: str | None = None
    source_url: str | None = None
    confirmation_text: str | None = None
    capture_tool: str | None = None
    source_id: str | None = None
    artifact_url: str | None = None
    email: dict[str, str] | None = None


class QueueHold(HealthModel):
    """Why a queued job waits for the user before any dispatch may pick it."""

    kind: Literal["captcha"]
    detail: str
    page_url: str | None = None
    created_at: str


class QueueJob(HealthModel):
    """Public job identity and stored queue state without application answers."""

    job_id: int
    company: str | None = None
    title: str | None = None
    queue_state: str
    manual_priority: bool = False
    hold: QueueHold | None = None
    owner_thread_id: str | None = None
    next_retry_at: str | None = None
    retry_count: int = Field(ge=0)


class QueueHealth(HealthModel):
    """Queue availability, totals, and bounded job rows from one database read."""

    state: Availability
    total: int | None = None
    counts_by_state: dict[str, int] = Field(default_factory=dict)
    jobs: list[QueueJob] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class MonitoringHealth(HealthModel):
    """Availability and bounded discovery count of saved batch observations."""

    state: Availability
    discovered_batches: int = 0
    truncated: bool = False
    error: str | None = None


class ObservationCounts(HealthModel):
    """Independent progress counts from one saved batch observation."""

    submitted: int = Field(ge=0)
    synced: int = Field(ge=0)
    closed: int = Field(ge=0)
    reserved: int = Field(ge=0)
    occupied: int = Field(ge=0)
    paused: int = Field(ge=0)


class ObservationUsage(HealthModel):
    """Nonnegative token and request totals from a saved job observation."""

    total_tokens: int = Field(ge=0)
    requests: int = Field(ge=0)


class ObservedJob(HealthModel):
    """Allowlisted progress evidence for one job in a saved batch observation."""

    job_id: int
    company: str = ""
    title: str = ""
    job_url: str | None = None
    queue_state: str | None = None
    turn_state: str | None = None
    owner_thread_id: str | None = None
    submitted: bool
    applier_label: str | None = None
    counted: bool = True
    user_submitted: bool = False
    synced: bool
    closed: bool
    paused: bool
    terminal: bool = False
    blocked: str | None = None
    result_status: str | None = None
    next_step: str | None = None
    last_tool_at: str | None = None
    turn_at: str | None = None
    elapsed_seconds: int | None = Field(default=None, ge=0)
    errors: list[str] = Field(default_factory=list)
    usage: ObservationUsage | None = None


class BlockerCounts(HealthModel):
    """Saved counts of controller, user, and result waits for one batch."""

    pending_controller: int
    awaiting_user: int
    waiting_result: int


class QueueIssueHealth(HealthModel):
    """Saved infrastructure incident state without raw diagnostics or secrets."""

    state: Literal[
        "no_active_issue_recorded", "active_issue", "missing", "invalid", "unavailable"
    ]
    error_type: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    delivery_state: str | None = None


class ControllerHealth(HealthModel):
    """The controller thread of an application batch and its dispatch settings."""

    thread_id: str
    display_name: str
    dispatch_mode: Literal["controller", "separate_dispatcher"]
    dispatcher_thread_id: str | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=200)
    worker_status: Literal["launch_admitted", "stopped_by_user"] | None = None


class BatchHealth(HealthModel):
    """A bounded batch observation with source binding and freshness labels."""

    id: str
    source_binding: Literal["valid", "invalid", "missing"] = "missing"
    controller: ControllerHealth | None = None
    freshness: Literal["fresh", "stale", "missing", "invalid", "unavailable"]
    checked_at: str | None = None
    age_seconds: int | None = None
    complete: bool | None = None
    counts: ObservationCounts | None = None
    target: int | None = None
    jobs: list[ObservedJob] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    blockers: BlockerCounts | None = None
    queue_health: QueueIssueHealth
    jobs_truncated: bool = False


class ApplyHealthResponse(HealthModel):
    """Read-only queue and saved monitor observations for the shared dashboard."""

    checked_at: str
    mode: Literal["read_only"] = "read_only"
    runtime: RuntimeHealth
    queue: QueueHealth
    monitoring: MonitoringHealth
    batches: list[BatchHealth] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Saved observations do not prove a worker is currently running.",
            "Batch counts overlap and must not be added across batches.",
        ]
    )
