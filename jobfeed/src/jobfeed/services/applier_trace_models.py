"""Public shape of a Claude Applier run, as the dashboard's trace waterfall draws it."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

StepCategory = Literal["model", "browser", "backend", "shell", "human"]


class TraceModel(BaseModel):
    """Base model for trace payloads; serializes defaults as required fields."""

    model_config = ConfigDict(
        populate_by_name=True, json_schema_serialization_defaults_required=True
    )


class TraceStep(TraceModel):
    """One timed step. Times are seconds from the start of the run."""

    kind: Literal["step"]
    id: str
    name: str
    meta: str
    category: StepCategory
    start: float
    end: float
    outcome: Literal["ok", "failed"]
    fields_filled: int | None = Field(default=None, alias="fieldsFilled")
    note: str | None = None


class TraceGroup(TraceModel):
    """A worker or Applier span with nested groups and steps."""

    kind: Literal["group"]
    id: str
    name: str
    meta: str
    role: Literal["worker", "applier"]
    start: float
    end: float
    children: list[Annotated[TraceGroup | TraceStep, Field(discriminator="kind")]]


class ApplierTrace(TraceModel):
    """The trace tree of one Applier run, with its title and timing."""

    job_id: int
    started_at: str
    is_live: bool
    title: str
    subtitle: str
    fields_total: int | None = Field(default=None, alias="fieldsTotal")
    root: TraceGroup


class ApplierRun(TraceModel):
    """One Applier run as listed on the runs page."""

    job_id: int
    company: str | None
    title: str | None
    state: str
    started_at: str
    updated_at: str
    is_live: bool
    session_count: int


class ApplierRuns(TraceModel):
    """Applier runs listed on the runs page."""

    runs: list[ApplierRun]
    autopilot_enabled: bool
