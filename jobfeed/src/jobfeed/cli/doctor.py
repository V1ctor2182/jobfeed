"""`jobfeed doctor`: one read-only check that everything on this machine works.

It covers the dashboard, its background service, setup, the AI provider, the
latest scan and evaluation, the daily scan, signed-in job sites, the private
link, free disk and private data. Each line says what is wrong and how to fix
it. It never starts a scan; the AI check makes one tiny request (`--no-ai`
skips it).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path

import click
import httpx

from jobfeed.cli.onboard import DEFAULT_URL, Dashboard, open_dashboard
from jobfeed.cli.remote_access import DEFAULT_TUNNEL_METRICS
from jobfeed.doctor_app import (
    check_ai,
    check_dashboard,
    check_runs,
    setup_rows,
)
from jobfeed.doctor_host import Check, Host, Status
from jobfeed.doctor_sources import check_sources
from jobfeed.doctor_system import (
    check_daily_scan,
    check_disk,
    check_private_data,
    check_private_link,
    check_service,
)

_MARK: dict[Status, str] = {"ok": "✅", "warn": "⚠️", "fail": "❌", "off": "➖"}  # noqa: RUF001


def run_checks(  # noqa: PLR0913 - every input is replaceable in tests
    *,
    host: Host,
    root: Path,
    url: str,
    skip_ai: bool,
    metrics_url: str,
    connect: Callable[[str], AbstractContextManager[httpx.Client]] = open_dashboard,
) -> list[Check]:
    """Run every check; those that need the dashboard are skipped without it.

    Args:
        host: Command, disk and network access.
        root: Jobfeed project folder.
        url: Dashboard origin.
        skip_ai: Whether to skip the AI request.
        metrics_url: cloudflared metrics origin.
        connect: Opens a dashboard client.

    Returns:
        The report lines, in order.
    """
    rows: list[Check] = []
    daily = check_daily_scan(host, root)
    with connect(url) as client:
        dashboard: Dashboard | None = Dashboard(client)
        try:
            Dashboard(client).get("/api/health")
        except (httpx.HTTPError, click.ClickException):
            dashboard = None
        rows.append(check_dashboard(dashboard))
        rows.append(check_service(host))
        if dashboard is not None:
            rows.extend(setup_rows(dashboard))
            rows.append(check_ai(host, dashboard, skip=skip_ai))
            rows.extend(check_runs(dashboard, daily=daily.status != "off"))
            rows.extend(check_sources(dashboard))
    rows.append(daily)
    rows.append(check_private_link(host, root, metrics_url))
    rows.append(check_disk(host, root))
    rows.append(check_private_data(host, root))
    return rows


def _summary(rows: list[Check]) -> str:
    counts = {status: sum(row.status == status for row in rows) for status in _MARK}
    checked = counts["ok"] + counts["warn"] + counts["fail"]
    if counts["fail"]:
        return f"{counts['fail']} of {checked} checks failed."
    if counts["warn"]:
        return f"No failures; {counts['warn']} of {checked} checks need attention."
    return f"All {checked} checks passed."


def _project_root(ctx: click.Context) -> Path:
    app = ctx.find_root().obj
    config_path = app.get("config_path") if isinstance(app, dict) else None
    if config_path:
        return Path(config_path).resolve().parent
    return Path(__file__).resolve().parents[3]


@click.command(name="doctor")
@click.option(
    "--url",
    default=lambda: os.environ.get("JOBFEED_URL", DEFAULT_URL),
    show_default=DEFAULT_URL,
    help="Origin of the running Jobfeed dashboard.",
)
@click.option("--no-ai", "skip_ai", is_flag=True, help="Skip the tiny AI request.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON for agents.")
@click.option(
    "--tunnel-metrics",
    default=lambda: os.environ.get("JOBFEED_TUNNEL_METRICS", DEFAULT_TUNNEL_METRICS),
    show_default=DEFAULT_TUNNEL_METRICS,
    help="cloudflared metrics address that reports the private link.",
)
@click.pass_context
def doctor(
    ctx: click.Context, url: str, skip_ai: bool, as_json: bool, tunnel_metrics: str
) -> None:
    """Check that everything on this machine works. Read-only.

    Exits 1 when any check fails. Starts no scan.
    \f

    Args:
        ctx: Click invocation context.
        url: Dashboard origin.
        skip_ai: Whether to skip the AI request.
        as_json: Whether to print JSON.
        tunnel_metrics: cloudflared metrics origin.

    Raises:
        SystemExit: With status 1 when a check fails.
    """
    rows = run_checks(
        host=Host(),
        root=_project_root(ctx),
        url=url.rstrip("/"),
        skip_ai=skip_ai,
        metrics_url=tunnel_metrics,
    )
    failed = [row for row in rows if row.status == "fail"]
    if as_json:
        click.echo(
            json.dumps(
                {"ok": not failed, "checks": [asdict(row) for row in rows]},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for row in rows:
            click.echo(f"{_MARK[row.status]} {row.check}: {row.detail}")
            if row.fix and row.status in {"fail", "warn"}:
                click.echo(f"   → {row.fix}")
        click.echo("")
        click.echo(_summary(rows))
    if failed:
        raise SystemExit(1)


__all__ = [
    "Check",
    "Host",
    "check_runs",
    "check_sources",
    "doctor",
    "run_checks",
]
