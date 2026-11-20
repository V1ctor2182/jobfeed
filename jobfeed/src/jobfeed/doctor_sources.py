"""`jobfeed doctor` check that each job source found jobs in its latest scan."""

from __future__ import annotations

from typing import Any

from jobfeed.cli.onboard import Dashboard
from jobfeed.doctor_app import local_time
from jobfeed.doctor_host import Check

# Every job source: config field -> (label, scan progress key, needs a sign-in).
# Career page search and company discovery find job boards, not jobs, so a scan
# with no new jobs is normal for them.
_BOARD_FINDERS = {"ats-search", "ats-discovery"}
_SOURCES = {
    "ats": ("Company career pages", "ats", False),
    "ats_search": ("Career page search", "ats-search", False),
    "ats_discovery": ("New company discovery", "ats-discovery", False),
    "speedyapply": ("SpeedyApply job lists", "speedyapply", False),
    "speedrun": ("Speedrun job lists", "speedrun", False),
    "indeed": ("Indeed", "indeed", False),
    "linkedin_guest": ("LinkedIn public search", "linkedin_guest", False),
    "linkedin": ("LinkedIn (browser)", "linkedin", False),
    "handshake": ("Handshake", "handshake", True),
    "linkedin_extension": ("LinkedIn signed in", "linkedin-extension", True),
    "jobright": ("Jobright", "jobright", True),
}
_SIGNED_OUT = ("401", "sign in", "signed out", "login", "authenticated")
_FINISHED = {"completed", "completed_with_warnings"}
_EMPTY_SCANS = 3


def _source_row(
    label: str, key: str, signed_in: bool, runs: list[dict[str, Any]]
) -> Check:
    name = f"Source: {label}"
    scans = [r for r in runs if key in (r.get("scan_progress") or {})]
    if not scans:
        test = (
            "the first scan will test the sign-in" if signed_in else "not scanned yet"
        )
        return Check(name, "off", f"on; {test}")
    if scans[0]["status"] == "running":
        return Check(name, "ok", "scanning now")
    # A scan stopped by a restart says nothing about the source; use the latest
    # scan in which this source finished or failed.
    ended = [
        r
        for r in scans
        if str(r["scan_progress"][key].get("phase")) in _FINISHED | {"failed"}
    ]
    if not ended:
        when = local_time(scans[0]["started_at"])
        return Check(name, "warn", f"{when}: the scan stopped before it finished")
    scans = ended
    run = scans[0]
    progress = run["scan_progress"][key]
    phase = str(progress.get("phase") or "")
    message = str(progress.get("message") or "")
    when = local_time(run["started_at"])
    again = (
        f'Ask any coding agent: "sign me in to {label} again" '
        '(steps: "Signing in again" in docs/server-install.md).'
    )
    if phase in _FINISHED:
        stats = (run.get("scan_stats") or {}).get(key, {})
        if key in _BOARD_FINDERS:
            boards = stats.get("boards_added", 0)
            return Check(name, "ok", f"{when}: {boards} new career pages found")
        recent = [
            (r.get("scan_stats") or {}).get(key, {}).get("discovered", 0)
            for r in scans[:_EMPTY_SCANS]
            if str(r["scan_progress"][key].get("phase")) in _FINISHED
        ]
        if len(recent) == _EMPTY_SCANS and not any(recent):
            return Check(
                name,
                "warn",
                f"no jobs in the last {_EMPTY_SCANS} scans",
                again
                if signed_in
                else "Check its search titles, or whether the site blocks this server.",
            )
        return Check(
            name,
            "ok",
            f"{when}: {stats.get('discovered', 0)} found, "
            f"{stats.get('inserted', 0)} new",
        )
    lowered = message.lower()
    if signed_in and "not connected" in lowered:
        return Check(
            name,
            "fail",
            f"{when}: the Chrome extension was not connected",
            "Keep Chrome open on the server; the extension reconnects within 30 "
            "seconds.",
        )
    if signed_in and any(sign in lowered for sign in _SIGNED_OUT):
        return Check(name, "fail", f"{when}: signed out of {label}", again)
    return Check(
        name,
        "fail",
        f"{when}: failed ({message or 'no reason given'})",
        again
        if signed_in
        else "Scans still use the other sources. If it keeps failing, the site may "
        "be blocking this server; turn it off in Settings.",
    )


def check_sources(dashboard: Dashboard) -> list[Check]:
    """Each job source that is on found jobs in its latest scan.

    A source that failed is a failure; one that found nothing three scans in a
    row needs attention. Signed-in sites also report a sign-out.

    Args:
        dashboard: Client for the dashboard.

    Returns:
        One line per job source that is turned on.
    """
    configuration = dashboard.get("/api/config")
    if not configuration.get("configured"):
        return []
    sources = configuration["sources"]
    runs = [
        run
        for run in dashboard.get("/api/runs?limit=50").get("runs", [])
        if run["source"] != "evaluate"
    ]
    return [
        _source_row(label, key, signed_in, runs)
        for field, (label, key, signed_in) in _SOURCES.items()
        if sources.get(field, {}).get("enabled")
    ]


__all__ = ["check_sources"]
