"""Whether the application pipeline is installed beside this Jobfeed.

A server install sets up Jobfeed alone: it finds and ranks jobs. Review,
the answer bank and the application-email tracker run the pipeline's own
Python environment, which only the Claude Applier setup creates.
"""

NOT_INSTALLED_CODE = "applications_not_installed"
NOT_INSTALLED_MESSAGE = (
    "Applying isn't set up on this Jobfeed. It finds and ranks jobs; "
    "application questions and application email need the Claude Applier setup."
)


class ApplicationsNotInstalled(Exception):
    """The pipeline's Python environment does not exist."""

    def __init__(self) -> None:
        super().__init__(NOT_INSTALLED_MESSAGE)


__all__ = ["NOT_INSTALLED_CODE", "NOT_INSTALLED_MESSAGE", "ApplicationsNotInstalled"]
