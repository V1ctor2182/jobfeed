"""Whether the application pipeline is installed beside this Jobfeed.

A server install sets up Jobfeed alone: it finds and ranks jobs. Review and
the answer bank run the pipeline's own Python environment, which only the
Claude Applier setup creates. The application-email tracker needs only the
standard library and Jobfeed, so it also runs with Jobfeed's own Python.
"""

import os
from pathlib import Path

NOT_INSTALLED_CODE = "applications_not_installed"
NOT_INSTALLED_MESSAGE = (
    "Applying isn't set up on this Jobfeed. It finds and ranks jobs; "
    "application questions need the Claude Applier setup."
)


def default_pipeline_root() -> Path:
    """Return the application pipeline folder beside this Jobfeed.

    Returns:
        ``JOBFEED_APPLY_ROOT`` when set, else the sibling
        ``resume-application-pipeline`` folder.
    """
    override = os.environ.get("JOBFEED_APPLY_ROOT")
    if override is not None:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "resume-application-pipeline"


class ApplicationsNotInstalled(Exception):
    """The pipeline's Python environment does not exist."""

    def __init__(self) -> None:
        super().__init__(NOT_INSTALLED_MESSAGE)


__all__ = [
    "NOT_INSTALLED_CODE",
    "NOT_INSTALLED_MESSAGE",
    "ApplicationsNotInstalled",
    "default_pipeline_root",
]
