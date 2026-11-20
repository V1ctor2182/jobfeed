"""`jobfeed email`: track replies to applications from Gmail, read-only.

Gmail is reached through the Gmail connector of the Claude account signed in on
this machine, one `claude -p` call at a time. Nothing is sent, labelled or
deleted in Gmail.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from jobfeed.applications_runtime import default_pipeline_root
from jobfeed.tracker import TrackerError, TrackerService, check_gmail, sync_mail


def _service(ctx: click.Context) -> TrackerService:
    app = ctx.find_root().obj
    settings = app.get("settings") if isinstance(app, dict) else None
    database = Path(settings.db.path) if settings else Path("data/jobfeed.sqlite")
    return TrackerService(default_pipeline_root(), database)


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@click.group(name="email")
def email() -> None:
    """Track replies to your applications from Gmail (read-only)."""


@email.command(name="check")
@click.pass_context
def email_check(ctx: click.Context) -> None:
    """Make one small Gmail search to prove the connector works. Saves nothing.
    \f

    Args:
        ctx: Click invocation context.

    Raises:
        SystemExit: With status 1 when Gmail does not answer.
    """
    result = check_gmail(_service(ctx))
    _emit(result.model_dump(exclude_none=True))
    if not result.connected:
        raise SystemExit(1)


@email.command(name="sync")
@click.pass_context
def email_sync(ctx: click.Context) -> None:
    """Save new application email and sort it by stage. Sends nothing.

    The first run looks back 45 days; later runs pick up where the last one
    ended. The background timer runs this every 30 minutes.
    \f

    Args:
        ctx: Click invocation context.

    Raises:
        SystemExit: With status 1 when Gmail could not be read.
    """
    result = sync_mail(_service(ctx))
    _emit(result)
    if "error" in result:
        raise SystemExit(1)


@email.command(name="status")
@click.pass_context
def email_status(ctx: click.Context) -> None:
    """Show the last mail sync and how much application email is saved.
    \f

    Args:
        ctx: Click invocation context.

    Raises:
        click.ClickException: If the saved email cannot be read.
    """
    try:
        state = _service(ctx).state()
    except TrackerError as exc:
        raise click.ClickException(str(exc)) from None
    _emit(
        {
            "last_sync": state.last_sync.model_dump() if state.last_sync else None,
            "messages": len(state.messages),
            "applications": len(state.applications),
        }
    )


__all__ = ["email"]
