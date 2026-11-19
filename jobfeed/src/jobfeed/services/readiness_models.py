"""Bounded public results for harmless application readiness checks."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ReadinessCheck(BaseModel):
    """One required or informational prerequisite, without personal data."""

    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(max_length=64)
    label: str = Field(max_length=128)
    status: Literal["ready", "blocked", "not_tested"]
    required: bool
    checked_at: str | None
    reason: str = Field(max_length=512)
    next_step: str = Field(default="", max_length=1024)


class ReadinessRun(BaseModel):
    """A persisted diagnostic run whose readiness expires after five minutes."""

    model_config = ConfigDict(strict=True, extra="forbid")
    run_id: str
    phase: Literal[
        "running", "completed", "blocked", "dispatch_unknown", "cleanup_unknown"
    ]
    started_at: str
    finished_at: str | None
    expires_at: str | None
    stale: bool
    ready: bool
    checks: list[ReadinessCheck] = Field(max_length=32)


class ReadinessSnapshot(BaseModel):
    """The latest run and a server-scoped mutation token."""

    csrf_token: str
    run: ReadinessRun | None


class ReadinessProvider(Protocol):
    """Execute or observe a diagnostic without authorizing application work."""

    def start(self, request_id: str, origin: str) -> ReadinessRun:
        """Start or reuse one diagnostic.

        Args:
            request_id: Client-generated UUID for idempotency.
            origin: Validated loopback dashboard origin.

        Returns:
            The admitted or already active diagnostic.
        """
        ...

    def get(self) -> ReadinessRun | None:
        """Observe the latest diagnostic without creating or sending work.

        Returns:
            Latest run, or None before the first check.
        """
        ...

    def fixture(self, run_id: str) -> str:
        """Read the fixed synthetic fixture for one admitted run.

        Args:
            run_id: Server-generated diagnostic UUID.

        Returns:
            Static HTML with no upload or submission destination.

        Raises:
            ValueError: If the run is unknown or malformed.
        """
        ...
