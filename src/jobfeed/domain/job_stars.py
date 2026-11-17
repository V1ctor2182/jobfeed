"""User annotations remain separate from application queue authority."""

from dataclasses import dataclass


@dataclass(frozen=True)
class JobStar:
    """Whether a source job is starred, with its real job if linked."""

    source_job_id: str
    real_job_id: str | None
    starred: bool
