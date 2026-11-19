"""Filesystem configuration for the local resume pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_APPLICATION_MODEL = "gpt-6.1-sol"
DEFAULT_APPLICATION_EFFORT = "low"


@dataclass(frozen=True)
class PipelineConfig:
    resume_root: Path
    jobfeed_root: Path
    jobfeed_db: Path
    applications_root: Path
    application_model: str = DEFAULT_APPLICATION_MODEL
    application_effort: str = DEFAULT_APPLICATION_EFFORT

    @classmethod
    def discover(cls, resume_root: Path | None = None) -> "PipelineConfig":
        root = (resume_root or Path(__file__).resolve().parents[1]).resolve()
        jobfeed = root.parent / "jobfeed"
        return cls(
            resume_root=root,
            jobfeed_root=jobfeed,
            jobfeed_db=jobfeed / "data" / "jobfeed.sqlite",
            applications_root=root / "applications",
        )


def project_directory(runtime_root: Path, project_root: str | Path | None = None) -> Path:
    runtime = Path(runtime_root).resolve()
    if project_root is None:
        return runtime
    project = Path(project_root)
    if not project.is_absolute():
        raise ValueError("project_root must be absolute")
    project = project.resolve(strict=True)
    if not project.is_dir() or not runtime.is_relative_to(project):
        raise ValueError("project_root must contain the pipeline runtime")
    return project


def runtime_command(runtime_root: Path, argv: list[str]) -> str:
    import shlex
    return f"cd {shlex.quote(str(runtime_root))} && {shlex.join(argv)}"
