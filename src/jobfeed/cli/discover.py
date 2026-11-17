"""Click command that backfills discovered ATS companies without scanning jobs."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import click

from jobfeed.adapters.sources._ats_discovery import Board, LookupCache, OriginReport
from jobfeed.adapters.sources._http import create_http_client
from jobfeed.adapters.sources.ats_discovery import (
    discovered_company,
    discovery_cache_path,
    failure_summary,
    new_boards,
    run_discovery,
)
from jobfeed.cli import AppContext, require_app, run_with_store
from jobfeed.cli.companies import _store_ops


@click.command(
    name="discover-companies",
    help="Find company ATS boards on VC portfolio boards and Hacker News.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Write new companies.")
@click.option(
    "--max-lookups",
    type=click.IntRange(min=1),
    default=None,
    help="Lookups this run (default: sources.ats_discovery.max_lookups_per_scan).",
)
@click.pass_context
def discover_companies(
    ctx: click.Context, apply_changes: bool, max_lookups: int | None
) -> None:
    """Discover ATS boards and preview or add the new companies (dry-run default).

    Lookup answers are cached even in a dry run; only company rows wait for
    ``--apply``. No jobs are fetched.

    Args:
        ctx: Click invocation context.
        apply_changes: Whether to add new companies (else preview only).
        max_lookups: Override of the per-run lookup budget.
    """
    app = require_app(ctx)
    asyncio.run(_run(app, apply_changes=apply_changes, max_lookups=max_lookups))


async def _run(
    app: AppContext, *, apply_changes: bool, max_lookups: int | None
) -> None:
    settings = app["settings"]
    config = settings.sources.ats_discovery
    cache_path = discovery_cache_path(settings.db.path)
    started = time.monotonic()
    now = datetime.now(UTC)
    client = create_http_client(config.timeout_s)
    try:
        reports = await run_discovery(
            config, client, cache_path, now=now, max_lookups=max_lookups
        )
    finally:
        await client.aclose()
    if not reports:
        raise click.ClickException("sources.ats_discovery configures no origin")
    for line in failure_summary(reports):
        click.echo(f"warning: {line}", err=True)
    await run_with_store(
        app, lambda: _report_and_write(app, reports, now, apply_changes=apply_changes)
    )
    entries = len(LookupCache.load(cache_path).entries)
    click.echo(f"Lookup cache: {entries} entries in {cache_path}")
    click.echo(f"Finished in {time.monotonic() - started:.0f}s.")


async def _report_and_write(
    app: AppContext,
    reports: list[OriginReport],
    now: datetime,
    *,
    apply_changes: bool,
) -> None:
    tracked = await _store_ops(app).list_companies(include_removed=True)
    for report in reports:
        new = len(new_boards(report.boards, tracked))
        slugs = len({board.slug for board in report.boards})
        click.echo(
            f"{report.name}: {report.listed} listed, {report.looked_up} looked up, "
            f"{report.cached} cached, {report.deferred} deferred, "
            f"{report.lookup_errors} lookup errors, {len(report.boards)} on an ATS, "
            f"{new} new, {slugs - new} already tracked"
        )
    boards: set[Board] = set().union(*(r.boards for r in reports))
    to_add = new_boards(boards, tracked)
    skipped = len({b.slug for b in boards}) - len(to_add)
    if not apply_changes:
        for board in to_add:
            click.echo(f"  would add {board.slug} ({board.vendor})")
        click.echo(f"Dry-run: {len(to_add)} new, {skipped} already tracked.")
        return
    for board in to_add:
        await _store_ops(app).upsert_company(discovered_company(board, now))
    click.echo(f"Added {len(to_add)} new, {skipped} already tracked.")


__all__ = ["discover_companies"]
