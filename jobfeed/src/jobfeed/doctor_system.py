"""`jobfeed doctor` checks of this machine: service, schedule, link, disk, git."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from jobfeed.doctor_host import (
    DASHBOARD_AGENT,
    DASHBOARD_UNIT,
    SCAN_AGENT,
    SCAN_SERVICE,
    SCAN_TIMER,
    Check,
    Host,
    launch_agent_loaded,
    stdout_of,
    systemd,
)
from jobfeed.remote_access import RemoteAccessStore

_LOW_DISK = 2 * 1024**3
_FULL_DISK = 1024**3
_UNAUTHORIZED = 401
_OK = 200


def check_service(host: Host) -> Check:
    """The dashboard runs as a background service that survives a reboot.

    Args:
        host: Command runner.

    Returns:
        The report line.
    """
    name = "Background service"
    if host.platform == "darwin":
        if launch_agent_loaded(host, DASHBOARD_AGENT):
            return Check(name, "ok", "running, starts again after a restart")
        return Check(
            name,
            "warn",
            "not installed as a background service",
            "Follow step 3 of docs/server-install.md.",
        )
    enabled = systemd(host, "is-enabled", DASHBOARD_UNIT)
    if enabled in {"", "not-found"}:
        return Check(
            name,
            "warn",
            "not installed as a background service",
            "Follow step 3 of docs/server-install.md.",
        )
    active = systemd(host, "is-active", DASHBOARD_UNIT)
    if active != "active":
        return Check(
            name,
            "fail",
            f"service is {active}",
            f"systemctl --user restart {DASHBOARD_UNIT}",
        )
    if enabled != "enabled":
        return Check(
            name,
            "fail",
            "running, but will not start after a restart",
            f"systemctl --user enable {DASHBOARD_UNIT}",
        )
    user = os.environ.get("USER", "")
    linger = stdout_of(
        host.run("loginctl", "show-user", user, "-p", "Linger", "--value")
    )
    if linger == "no":
        return Check(
            name,
            "warn",
            "running, but stops when you log out",
            f"sudo loginctl enable-linger {user}",
        )
    return Check(name, "ok", "running, starts again after a restart")


def service_path(host: Host) -> str:
    """Return the PATH the dashboard's background service runs with.

    Args:
        host: Command runner.

    Returns:
        The service PATH, or this process's PATH when it cannot be read.
    """
    if host.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{DASHBOARD_AGENT}.plist"
        try:
            with plist.open("rb") as stream:
                return str(plistlib.load(stream)["EnvironmentVariables"]["PATH"])
        except (OSError, KeyError, plistlib.InvalidFileException):
            return os.environ.get("PATH", "")
    environment = systemd(host, "show", DASHBOARD_UNIT, "-p", "Environment", "--value")
    for item in environment.split():
        if item.startswith("PATH="):
            return item.removeprefix("PATH=")
    return os.environ.get("PATH", "")


def _last_cycle(root: Path) -> str:
    try:
        lines = (root / "data/scheduler/cycle.log").read_text().splitlines()
    except OSError:
        return ""
    return next(
        (
            line
            for line in reversed(lines)
            if "Cycle result=" in line or "Cycle failed" in line or "Skipped" in line
        ),
        "",
    )


def check_daily_scan(host: Host, root: Path) -> Check:
    """The daily scan is scheduled and its last run did not fail.

    Args:
        host: Command runner.
        root: Jobfeed project folder.

    Returns:
        The report line.
    """
    name = "Daily scan"
    if host.platform == "darwin":
        if not launch_agent_loaded(host, SCAN_AGENT):
            return Check(name, "off", "off")
        detail, result = "scheduled", ""
    else:
        if systemd(host, "is-enabled", SCAN_TIMER) != "enabled":
            return Check(name, "off", "off")
        next_run = systemd(
            host, "show", SCAN_TIMER, "-p", "NextElapseUSecRealtime", "--value"
        )
        detail = f"next run {next_run} (server time)" if next_run else "scheduled"
        result = systemd(host, "show", SCAN_SERVICE, "-p", "Result", "--value")
    last = _last_cycle(root)
    if (result and result != "success") or "Cycle failed" in last:
        return Check(
            name,
            "fail",
            f"{detail}; the last run failed",
            f"Read data/scheduler/cycle.log, or `journalctl --user -u {SCAN_SERVICE}`.",
        )
    return Check(name, "ok", detail)


def check_private_link(host: Host, root: Path, metrics_url: str) -> Check:
    """The private link, when on, asks for the access code.

    Args:
        host: Command runner.
        root: Jobfeed project folder.
        metrics_url: cloudflared metrics origin.

    Returns:
        The report line.
    """
    name = "Private link"
    access = RemoteAccessStore(root / "data" / "remote-access.json").load()
    tunnel = host.get(f"{metrics_url.rstrip('/')}/quicktunnel")
    hostname = None
    if tunnel is not None and tunnel.is_success:
        try:
            hostname = tunnel.json().get("hostname")
        except ValueError:
            hostname = None
    link = f"https://{hostname}" if hostname else None
    if access is None:
        if link:
            return Check(
                name,
                "warn",
                "a tunnel runs but the access code is off, so it refuses everyone",
            )
        return Check(name, "off", "off")
    if link is None:
        return Check(
            name,
            "warn",
            "the access code is on, but no tunnel link was found",
            "systemctl --user restart jobfeed-tunnel.service",
        )
    response = host.get(f"{link}/api/health")
    if response is None:
        return Check(name, "warn", f"{link} does not answer yet")
    if response.status_code == _OK:
        return Check(
            name,
            "fail",
            f"{link} opens without the access code",
            "Stop it now: systemctl --user disable --now jobfeed-tunnel.service",
        )
    if response.status_code == _UNAUTHORIZED:
        return Check(name, "ok", f"{link} asks for the access code")
    return Check(name, "warn", f"{link} answered {response.status_code}")


def check_disk(host: Host, root: Path) -> Check:
    """There is room for the database and scans.

    Args:
        host: Disk reader.
        root: Jobfeed project folder.

    Returns:
        The report line.
    """
    free = host.free_bytes(root)
    detail = f"{free / 1024**3:.1f} GB free"
    if free < _FULL_DISK:
        return Check("Disk", "fail", detail, "Free up disk space on this machine.")
    if free < _LOW_DISK:
        return Check("Disk", "warn", detail, "Free up disk space soon.")
    return Check("Disk", "ok", detail)


def check_private_data(host: Host, root: Path) -> Check:
    """The database, résumé and settings stay out of git.

    Args:
        host: Command runner.
        root: Jobfeed project folder.

    Returns:
        The report line.
    """
    name = "Private data"
    ignored = host.run(
        "git", "-C", str(root), "check-ignore", "-q", "data/jobfeed.sqlite"
    )
    if ignored is None or ignored.returncode == 128:  # noqa: PLR2004 - not a git repository
        return Check(name, "off", "not a git checkout")
    if ignored.returncode != 0:
        return Check(
            name,
            "fail",
            "data/ is not ignored by git",
            "Restore jobfeed/.gitignore with `git checkout -- .gitignore`.",
        )
    status = host.run("git", "-C", str(root), "status", "--porcelain", "--", ".")
    changed = [line for line in (status.stdout if status else "").splitlines() if line]
    if changed:
        files = ", ".join(line[3:] for line in changed[:3])
        return Check(
            name,
            "warn",
            f"private data is ignored, but the checkout has local changes: {files}",
            "Updates may stop until these changes are committed or undone.",
        )
    return Check(name, "ok", "the database, résumé and settings are not tracked by git")


__all__ = [
    "check_daily_scan",
    "check_disk",
    "check_private_data",
    "check_private_link",
    "check_service",
    "service_path",
]
