"""Turn the dashboard's tunnel access code on or off, and find the tunnel link."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import httpx

from jobfeed.remote_access import RemoteAccessStore

# `cloudflared tunnel --metrics 127.0.0.1:7655 ...` reports its quick-tunnel
# hostname there; docs/server-install.md runs it that way.
DEFAULT_TUNNEL_METRICS = "http://127.0.0.1:7655"


def _store(ctx: click.Context) -> RemoteAccessStore:
    app = ctx.find_root().obj
    config_path = app.get("config_path") if isinstance(app, dict) else None
    root = Path(config_path).resolve().parent if config_path else Path.cwd()
    return RemoteAccessStore(root / "data" / "remote-access.json")


def _tunnel_link(metrics_url: str) -> str | None:
    try:
        response = httpx.get(
            f"{metrics_url.rstrip('/')}/quicktunnel", timeout=3.0, trust_env=False
        )
        hostname = response.json().get("hostname") if response.is_success else None
    except (httpx.HTTPError, ValueError):
        return None
    return f"https://{hostname}" if hostname else None


@click.group(name="remote-access")
def remote_access() -> None:
    """Let a browser open the dashboard through a tunnel, with an access code.

    Requests made on this machine never need the code. Requests that arrive
    through a tunnel (Cloudflare Tunnel, Tailscale Funnel) need it once per
    browser, and are refused while remote access is off.
    """


@remote_access.command(name="on")
@click.pass_context
def remote_access_on(ctx: click.Context) -> None:
    """Create a new access code and print it once.

    Running it again replaces the code and signs every browser out.
    \f

    Args:
        ctx: Click invocation context.
    """
    code = _store(ctx).create()
    click.echo(json.dumps({"remote_access": "on", "code": code}, indent=2))


@remote_access.command(name="off")
@click.pass_context
def remote_access_off(ctx: click.Context) -> None:
    """Refuse every request that arrives through a tunnel.
    \f

    Args:
        ctx: Click invocation context.
    """
    was_on = _store(ctx).delete()
    click.echo(json.dumps({"remote_access": "off", "was_on": was_on}, indent=2))


@remote_access.command(name="status")
@click.option(
    "--tunnel-metrics",
    default=lambda: os.environ.get("JOBFEED_TUNNEL_METRICS", DEFAULT_TUNNEL_METRICS),
    show_default=DEFAULT_TUNNEL_METRICS,
    help="cloudflared metrics address that reports the quick-tunnel link.",
)
@click.pass_context
def remote_access_status(ctx: click.Context, tunnel_metrics: str) -> None:
    """Show whether remote access is on and the current tunnel link.
    \f

    Args:
        ctx: Click invocation context.
        tunnel_metrics: cloudflared metrics origin.
    """
    on = _store(ctx).load() is not None
    click.echo(
        json.dumps(
            {
                "remote_access": "on" if on else "off",
                "tunnel_link": _tunnel_link(tunnel_metrics),
            },
            indent=2,
        )
    )


__all__ = ["remote_access"]
