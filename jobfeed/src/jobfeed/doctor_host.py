"""What `jobfeed doctor` reads from this machine, and the shape of its report."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

Status = Literal["ok", "warn", "fail", "off"]
DASHBOARD_UNIT = "jobfeed-dashboard.service"
SCAN_TIMER = "jobfeed-daily-scan.timer"
SCAN_SERVICE = "jobfeed-daily-scan.service"
DASHBOARD_AGENT = "com.jobfeed.dashboard"
SCAN_AGENT = "com.jobfeed.daily-scan"


@dataclass(frozen=True)
class Check:
    """One line of the report."""

    check: str
    status: Status
    detail: str
    fix: str = ""


class Host:
    """The commands and files the checks read. Tests replace it."""

    platform = sys.platform

    def run(
        self, *command: str, timeout: float = 15, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str] | None:
        """Run a command; return None when it is not installed or hangs.

        Args:
            command: Program and arguments.
            timeout: Seconds before giving up.
            env: Environment to use instead of this process's.

        Returns:
            The finished process, or None.
        """
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def free_bytes(self, path: Path) -> int:
        """Return the free disk space under ``path``.

        Args:
            path: Any path on the disk to measure.

        Returns:
            Free bytes.
        """
        return shutil.disk_usage(path).free

    def get(self, url: str) -> httpx.Response | None:
        """GET a URL without proxy settings; None when it cannot be reached.

        Args:
            url: Address to fetch.

        Returns:
            The response, or None.
        """
        try:
            return httpx.get(url, timeout=10.0, trust_env=False)
        except httpx.HTTPError:
            return None


def stdout_of(result: subprocess.CompletedProcess[str] | None) -> str:
    """Return a finished command's trimmed output, or "" when it did not run.

    Args:
        result: The finished command, or None.

    Returns:
        Its standard output without surrounding whitespace.
    """
    return result.stdout.strip() if result is not None else ""


def systemd(host: Host, *args: str) -> str:
    """Return the output of one `systemctl --user` command.

    Args:
        host: Command runner.
        args: systemctl arguments.

    Returns:
        Its trimmed output.
    """
    return stdout_of(host.run("systemctl", "--user", *args))


def launch_agent_loaded(host: Host, label: str) -> bool:
    """Return whether a launchd agent is loaded for this user.

    Args:
        host: Command runner.
        label: The agent's label.

    Returns:
        True when `launchctl print` finds it.
    """
    result = host.run("launchctl", "print", f"gui/{os.getuid()}/{label}")
    return result is not None and result.returncode == 0


__all__ = [
    "DASHBOARD_AGENT",
    "DASHBOARD_UNIT",
    "SCAN_AGENT",
    "SCAN_SERVICE",
    "SCAN_TIMER",
    "Check",
    "Host",
    "Status",
    "launch_agent_loaded",
    "stdout_of",
    "systemd",
]
