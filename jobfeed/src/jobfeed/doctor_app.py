"""`jobfeed doctor` checks that need the running dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta

from jobfeed.cli.onboard import Dashboard, setup_checks
from jobfeed.doctor_host import Check, Host
from jobfeed.doctor_system import service_environment

_AI_TIMEOUT_S = 120
_STALE_SCAN = timedelta(hours=36)
_FIXES = {
    "setup finished": "Run `jobfeed onboard status` and finish its next_step.",
    "AI provider": "Sign the AI in again, then run `jobfeed onboard provider <name>`.",
    "résumé": "Upload a résumé with `jobfeed onboard resume <file>`.",
    "job sources": "Turn on searches with `jobfeed onboard searches --enable-all`.",
    "scan API": "Restart the dashboard service.",
    "Chrome extension": (
        "Open Chrome on this machine and keep it open; the extension reconnects "
        "within 30 seconds."
    ),
}


def check_dashboard(dashboard: Dashboard | None) -> Check:
    """The dashboard answers and its database is readable.

    Args:
        dashboard: Client for the dashboard, or None when it does not answer.

    Returns:
        The report line.
    """
    if dashboard is None:
        return Check(
            "Dashboard",
            "fail",
            "does not answer on this machine",
            "Restart the dashboard service, then read its log.",
        )
    health = dashboard.get("/api/health")
    if health.get("status") != "ok":
        return Check("Dashboard", "fail", f"database {health.get('db')}")
    return Check("Dashboard", "ok", "running, database ok")


def setup_rows(dashboard: Dashboard) -> list[Check]:
    """Setup, provider, résumé, job sources, scan API and extension.

    Args:
        dashboard: Client for the dashboard.

    Returns:
        One report line per setup check.
    """
    rows = []
    for row in setup_checks(dashboard):
        if row["check"] == "dashboard":
            continue
        name = str(row["check"])
        ok = bool(row["ok"])
        rows.append(
            Check(
                name[:1].upper() + name[1:],
                "ok" if ok else "fail",
                str(row["detail"]),
                "" if ok else _FIXES.get(name, ""),
            )
        )
    return rows


def check_ai(host: Host, dashboard: Dashboard, *, skip: bool) -> Check:
    """The AI provider answers one tiny request in the service's environment.

    Args:
        host: Command runner.
        dashboard: Client for the dashboard.
        skip: Whether to skip the request.

    Returns:
        The report line.
    """
    name = "AI answers"
    if skip:
        return Check(name, "off", "skipped (--no-ai)")
    provider = dashboard.get("/api/onboarding/provider")
    kind, model = provider.get("provider"), provider.get("quick_model")
    if kind == "claude_cli":
        command = ["claude", "-p", "Reply with OK", "--model", str(model)]
        login = "claude auth login"
    elif kind == "codex_cli":
        command = ["codex", "exec", "--skip-git-repo-check", "-m", str(model)]
        command.append("Reply with OK")
        login = "codex login --device-auth"
    else:
        return Check(name, "off", "API key providers are checked by each scan")
    result = host.run(*command, timeout=_AI_TIMEOUT_S, env=service_environment(host))
    if result is None:
        return Check(
            name,
            "fail",
            f"the background service cannot find `{command[0]}`",
            "Install it, then rebuild the service PATH (step 3 of the install doc).",
        )
    if result.returncode == 0 and "OK" in result.stdout.upper():
        return Check(name, "ok", f"{kind} answered with {model}")
    reason = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no reply"]
    return Check(name, "fail", reason[0][:160], f"Sign in again: `{login}`.")


def local_time(value: str) -> str:
    """Return an ISO time as this machine's local date and time.

    Args:
        value: ISO 8601 timestamp.

    Returns:
        For example "Nov 20 08:06 UTC".
    """
    return datetime.fromisoformat(value).astimezone().strftime("%b %d %H:%M %Z")


def check_runs(dashboard: Dashboard, *, daily: bool) -> list[Check]:
    """The latest scan and evaluation succeeded, and scans are not stale.

    Args:
        dashboard: Client for the dashboard.
        daily: Whether the daily scan is on.

    Returns:
        One line for the last scan and one for the last evaluation.
    """
    runs = dashboard.get("/api/runs?limit=50").get("runs", [])
    rows = []
    for name, wanted in (("Last scan", False), ("Last evaluation", True)):
        run = next((r for r in runs if (r["source"] == "evaluate") == wanted), None)
        if run is None:
            rows.append(Check(name, "off", "none yet"))
            continue
        when = local_time(run["started_at"])
        if run["status"] == "running":
            rows.append(Check(name, "ok", f"running since {when}"))
        elif run.get("failure_code") == "interrupted":
            rows.append(
                Check(
                    name,
                    "warn",
                    f"{when}: stopped when the service restarted",
                    "Nothing to fix; the next scan runs normally.",
                )
            )
        elif run["status"] != "succeeded" and run.get("failed_stage") != "source":
            reason = run.get("failure_message") or run["status"]
            rows.append(
                Check(
                    name,
                    "fail",
                    f"{when}: {reason}",
                    "Open Scanner runs in the dashboard for details.",
                )
            )
        elif wanted:
            rows.append(Check(name, "ok", f"{when}, {run['jobs_scored']} jobs scored"))
        else:
            detail = (
                f"{when}, {run['jobs_discovered']} found, {run['jobs_inserted']} new"
            )
            if run["status"] != "succeeded":
                rows.append(
                    Check(
                        name,
                        "warn",
                        f"{detail}; {run.get('failed_source')} failed",
                        "The other sites were scanned; check that site's settings.",
                    )
                )
                continue
            started = datetime.fromisoformat(run["started_at"])
            stale = daily and datetime.now(started.tzinfo) - started > _STALE_SCAN
            rows.append(
                Check(
                    name,
                    "warn" if stale else "ok",
                    f"{detail}" + ("; no scan in the last 36 hours" if stale else ""),
                    "Check the daily scan below." if stale else "",
                )
            )
    return rows


__all__ = [
    "check_ai",
    "check_dashboard",
    "check_runs",
    "local_time",
    "setup_rows",
]
