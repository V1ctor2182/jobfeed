"""Saved form observations, distinct from submission confirmation."""

from typing import Literal

from pydantic import Field

from jobfeed.services.apply_health_models import HealthModel

FormValue = str | bool | int | float | list[str] | None


class FormField(HealthModel):
    """One form question with the observed and written values."""

    id: str
    question: str
    value: FormValue = None
    state: Literal["observed", "written_unverified", "unanswered", "unavailable"]
    written_value: FormValue = None
    page_url: str | None = None
    section: str | None = None
    captured_at: str | None = None


class FormResume(HealthModel):
    """The résumé file chosen on a form and its upload status."""

    filename: str
    label: str | None = None
    artifact_url: str | None = None
    upload_status: Literal["selected", "upload_pending", "uploaded", "unknown"]


class FormSnapshot(HealthModel):
    """Form fields and résumé captured from one application attempt."""

    id: str
    captured_at: str | None = None
    owner_thread_id: str | None = None
    attempt_id: str | None = None
    provenance: Literal["captured", "legacy"]
    coverage: Literal["visible_controls", "partial_history"]
    source_url: str | None = None
    fields: list[FormField] = Field(default_factory=list)
    resume: FormResume | None = None


class ApplicationFormResponse(HealthModel):
    """Saved form snapshots for a job, or why none are available."""

    job_id: int
    status: Literal["available", "partial", "missing", "unavailable"]
    reason: str | None = None
    snapshots: list[FormSnapshot] = Field(default_factory=list)
