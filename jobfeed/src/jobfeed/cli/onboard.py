"""Click commands that drive first-run onboarding through the running dashboard.

A coding agent on a headless server cannot click through the setup screens, so
these commands call the same local API the screens call. Every step still needs
the user's own choices: the agent relays them from chat. Output is JSON so the
agent can read it back to the user.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO, cast

import click
import httpx

DEFAULT_URL = "http://127.0.0.1:7654"
_SHORT_TIMEOUT = 30.0
# Résumé analysis and company recommendation each make one model call.
_MODEL_TIMEOUT = 600.0
_PROVIDERS = (
    "claude_cli",
    "codex_cli",
    "anthropic_api",
    "openai_api",
    "azure_openai",
    "amazon_bedrock",
)
_EDITABLE_SECTIONS = (
    "llm",
    "scoring",
    "sources",
    "hard_filters",
    "ml_gate",
    "seniority_gate",
)

Json = dict[str, Any]


class Dashboard:
    """Thin JSON client for the local dashboard's onboarding API."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(self, path: str, *, timeout: float = _SHORT_TIMEOUT) -> Json:
        """Return the decoded JSON body of a GET request.

        Args:
            path: API path below the dashboard origin.
            timeout: Seconds to wait for the response.

        Returns:
            Decoded JSON object.
        """
        return _decode(self._client.get(path, timeout=timeout))

    def send(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout: float = _SHORT_TIMEOUT,
    ) -> Json:
        """Return the decoded JSON body of a write request.

        Args:
            method: HTTP method.
            path: API path below the dashboard origin.
            body: Optional JSON body.
            files: Optional multipart upload.
            timeout: Seconds to wait for the response.

        Returns:
            Decoded JSON object.
        """
        response = self._client.request(
            method, path, json=body, files=files, timeout=timeout
        )
        return _decode(response)


def _decode(response: httpx.Response) -> Json:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.is_success and isinstance(payload, dict):
        return payload
    message = f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
        elif payload.get("detail"):
            message = json.dumps(payload["detail"])
    raise click.ClickException(f"{response.request.url.path}: {message}")


_client_factory: Callable[..., httpx.Client] = httpx.Client


@contextmanager
def open_dashboard(url: str) -> Iterator[httpx.Client]:
    """Open a client for the dashboard on this machine.

    Args:
        url: Dashboard origin.

    Returns:
        Context manager for a client that sends the dashboard's own origin and
        no proxy settings.
    """
    # Profile writes only accept the dashboard's own origin. This command runs
    # on the same machine as the dashboard, so it sends that origin, and it
    # ignores the shell's proxy settings, which must not route loopback calls.
    with _client_factory(
        base_url=url, headers={"Origin": url}, trust_env=False
    ) as client:
        yield client


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _dashboard(ctx: click.Context) -> Dashboard:
    return cast(Dashboard, ctx.obj)


@click.group(name="onboard")
@click.option(
    "--url",
    default=lambda: os.environ.get("JOBFEED_URL", DEFAULT_URL),
    show_default=DEFAULT_URL,
    help="Origin of the running Jobfeed dashboard.",
)
@click.pass_context
def onboard(ctx: click.Context, url: str) -> None:
    """Complete first-run setup from a terminal or a coding agent.

    The dashboard must be running. Run `jobfeed onboard status` to see the
    next step.
    \f

    Args:
        ctx: Click invocation context.
        url: Dashboard origin.
    """
    client = ctx.with_resource(open_dashboard(url.rstrip("/")))
    ctx.obj = Dashboard(client)


@onboard.command(name="status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show what is done and the next onboarding step.
    \f

    Args:
        ctx: Click invocation context.
    """
    _emit(_status(_dashboard(ctx)))


def _status(dashboard: Dashboard) -> Json:
    configured = bool(dashboard.get("/api/config").get("configured"))
    provider = dashboard.get("/api/onboarding/provider")
    resume = dashboard.get("/api/onboarding/resume")
    enabled: list[str] = []
    if resume.get("is_confirmed"):
        searches = dashboard.get("/api/onboarding/searches").get("searches", [])
        enabled = [
            f"{row['source']}: {row['query']}" for row in searches if row["enabled"]
        ]
    companies = dashboard.get("/api/companies").get("companies", [])
    steps = {
        "provider": bool(
            provider.get("connected")
            and provider.get("quick_model")
            and provider.get("detailed_model")
        ),
        "resume": resume.get("original_name") is not None,
        "profile_suggested": resume.get("profile") is not None,
        "profile_confirmed": bool(resume.get("is_confirmed")),
        "searches": bool(enabled),
        "finished": configured,
    }
    next_step = next((name for name, done in steps.items() if not done), "done")
    return {
        "next_step": next_step,
        "steps": steps,
        "provider": {
            "name": provider.get("provider"),
            "connected": provider.get("connected"),
            "quick_model": provider.get("quick_model"),
            "detailed_model": provider.get("detailed_model"),
        },
        "resume": resume.get("original_name"),
        "enabled_searches": enabled,
        "tracked_companies": sum(1 for row in companies if not row.get("removed")),
    }


@onboard.command(name="provider")
@click.argument("provider", type=click.Choice(_PROVIDERS))
@click.option(
    "--api-key-env",
    metavar="NAME",
    help="Read the API key from this environment variable.",
)
@click.option(
    "--api-key-stdin",
    is_flag=True,
    help="Read the API key from standard input (prompts when interactive).",
)
@click.option("--quick", "quick_model", help="Quick model id to save.")
@click.option("--detailed", "detailed_model", help="Detailed model id to save.")
@click.pass_context
def provider(  # noqa: PLR0913 - one parameter per Click option
    ctx: click.Context,
    provider: str,
    api_key_env: str | None,
    api_key_stdin: bool,
    quick_model: str | None,
    detailed_model: str | None,
) -> None:
    """Test a model provider and list its models; save models when given.

    claude_cli and codex_cli reuse this machine's signed-in CLI and need no
    key. The key is never printed.
    \f

    Args:
        ctx: Click invocation context.
        provider: Provider name.
        api_key_env: Environment variable that holds the API key.
        api_key_stdin: Whether to read the API key from standard input.
        quick_model: Quick model id to save.
        detailed_model: Detailed model id to save.

    Raises:
        click.UsageError: If only one model is given or the key is missing.
    """
    if (quick_model is None) != (detailed_model is None):
        raise click.UsageError("Give both --quick and --detailed, or neither.")
    body: Json = {"provider": provider}
    api_key = _api_key(api_key_env, api_key_stdin)
    if api_key is not None:
        body["api_key"] = api_key
    dashboard = _dashboard(ctx)
    state = dashboard.send(
        "POST", "/api/onboarding/provider/test", body=body, timeout=_MODEL_TIMEOUT
    )
    if state.get("connected") and quick_model and detailed_model:
        state = dashboard.send(
            "PUT",
            "/api/onboarding/provider/models",
            body={
                "provider": provider,
                "quick_model": quick_model,
                "detailed_model": detailed_model,
            },
        )
    _emit(
        {
            "provider": provider,
            "connected": state.get("connected"),
            "detail": state.get("detail"),
            "models": [model["id"] for model in state.get("models", [])],
            "quick_model": state.get("quick_model"),
            "detailed_model": state.get("detailed_model"),
        }
    )


def _api_key(env_name: str | None, from_stdin: bool) -> str | None:
    if env_name is not None:
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise click.UsageError(f"Environment variable {env_name} is empty.")
        return value
    if not from_stdin:
        return None
    if sys.stdin.isatty():
        value = click.prompt("API key", hide_input=True).strip()
    else:
        value = sys.stdin.readline().strip()
    if not value:
        raise click.UsageError("No API key was given on standard input.")
    return value


@onboard.command(name="resume")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--analyze/--no-analyze",
    default=True,
    show_default=True,
    help="Send the résumé to the Detailed model for a suggested job profile.",
)
@click.pass_context
def resume(ctx: click.Context, path: Path, analyze: bool) -> None:
    """Upload a PDF, DOCX, Markdown or text résumé and suggest a job profile.

    Analysis sends the résumé text to the chosen provider. Run it only after
    the user agreed to that.
    \f

    Args:
        ctx: Click invocation context.
        path: Résumé file on this machine.
        analyze: Whether to request the profile suggestion.
    """
    dashboard = _dashboard(ctx)
    state = dashboard.send(
        "POST",
        "/api/onboarding/resume",
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
    )
    if analyze:
        state = dashboard.send(
            "POST", "/api/onboarding/resume/analyze", timeout=_MODEL_TIMEOUT
        )
    text = state.get("extracted_text") or ""
    _emit(
        {
            "resume": state.get("original_name"),
            "extracted_characters": len(text),
            "extracted_preview": text[:400],
            "profile": state.get("profile"),
            "confirmed": state.get("is_confirmed"),
        }
    )


@onboard.command(name="profile")
@click.option(
    "--confirm",
    "confirm_path",
    type=click.File("r"),
    help="JSON job profile the user approved ('-' for standard input).",
)
@click.pass_context
def profile(ctx: click.Context, confirm_path: TextIO | None) -> None:
    """Print the suggested job profile, or save the user's approved version.

    Edit the printed JSON with the user's corrections, then pass it to
    --confirm. Every field must be present.
    \f

    Args:
        ctx: Click invocation context.
        confirm_path: Approved profile JSON.

    Raises:
        click.ClickException: If no profile has been suggested yet.
    """
    dashboard = _dashboard(ctx)
    if confirm_path is None:
        state = dashboard.get("/api/onboarding/resume")
        if state.get("profile") is None:
            raise click.ClickException(
                "No profile yet. Run `jobfeed onboard resume FILE` first."
            )
        _emit({"confirmed": state.get("is_confirmed"), "profile": state["profile"]})
        return
    try:
        body = json.load(confirm_path)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Profile is not valid JSON: {exc}") from exc
    if isinstance(body, dict) and isinstance(body.get("profile"), dict):
        body = body["profile"]
    state = dashboard.send("PUT", "/api/onboarding/profile", body=body)
    _emit({"confirmed": state.get("is_confirmed"), "profile": state.get("profile")})


@onboard.command(name="searches")
@click.option("--enable-all", is_flag=True, help="Enable every suggested search.")
@click.option(
    "--enable",
    "titles",
    multiple=True,
    metavar="TITLE",
    help="Enable the LinkedIn and Indeed searches for this title (repeatable).",
)
@click.pass_context
def searches(ctx: click.Context, enable_all: bool, titles: tuple[str, ...]) -> None:
    """List the searches built from the confirmed profile, or enable some.

    Searches come from the profile's desired titles. To search another title,
    add it to the profile and confirm again. Enabling replaces the previous
    selection.
    \f

    Args:
        ctx: Click invocation context.
        enable_all: Whether to enable every suggestion.
        titles: Titles whose searches to enable.

    Raises:
        click.UsageError: If a title matches no suggested search.
    """
    dashboard = _dashboard(ctx)
    rows: list[Json] = dashboard.get("/api/onboarding/searches")["searches"]
    if enable_all or titles:
        wanted = {title.casefold() for title in titles}
        known = {row["query"].casefold() for row in rows}
        missing = sorted(wanted - known)
        if missing:
            raise click.UsageError(
                f"No suggested search for: {', '.join(missing)}. "
                f"Suggested titles: {', '.join(sorted({r['query'] for r in rows}))}"
            )
        rows = [
            {**row, "enabled": enable_all or row["query"].casefold() in wanted}
            for row in rows
        ]
        rows = dashboard.send(
            "PUT", "/api/onboarding/searches", body={"searches": rows}
        )["searches"]
    _emit(
        [
            {
                "title": row["query"],
                "source": row["source"],
                "location": row["location"],
                "enabled": row["enabled"],
            }
            for row in rows
        ]
    )


@onboard.group(name="companies")
def companies() -> None:
    """Choose company career pages to scan (optional)."""


@companies.command(name="recommend")
@click.option("--refresh", is_flag=True, help="Ask the model for new suggestions.")
@click.pass_context
def companies_recommend(ctx: click.Context, refresh: bool) -> None:
    """Suggest companies for the profile and check which boards Jobfeed can read.
    \f

    Args:
        ctx: Click invocation context.
        refresh: Whether to bypass saved suggestions.
    """
    dashboard = _dashboard(ctx)
    path = "/api/onboarding/companies/recommend" + ("?refresh=true" if refresh else "")
    recommendations = dashboard.send("POST", path, timeout=_MODEL_TIMEOUT)[
        "recommendations"
    ]
    probes = _probe(dashboard, [row["slug"] for row in recommendations])
    _emit(
        [
            {
                "name": row["name"],
                "slug": row["slug"],
                "vendor": probe["vendor"],
                "why": row["rationale"],
            }
            for row, probe in zip(recommendations, probes, strict=True)
        ]
    )


@companies.command(name="add")
@click.argument("entries", nargs=-1, required=True)
@click.pass_context
def companies_add(ctx: click.Context, entries: tuple[str, ...]) -> None:
    """Track companies by slug, name or Greenhouse/Ashby/Lever board URL.
    \f

    Args:
        ctx: Click invocation context.
        entries: Company slugs, names or board URLs.
    """
    dashboard = _dashboard(ctx)
    probes = _probe(dashboard, list(entries))
    rows = [
        {"slug": probe["slug"], "vendor": probe["vendor"]}
        for probe in probes
        if probe["vendor"] and probe["slug"]
    ]
    inserted = 0
    if rows:
        inserted = dashboard.send("POST", "/api/companies/bulk", body={"rows": rows})[
            "inserted"
        ]
    _emit(
        {
            "added": inserted,
            "already_tracked": len(rows) - inserted,
            "not_found": [probe["input"] for probe in probes if not probe["vendor"]],
        }
    )


@companies.command(name="catalog")
@click.option("--add", "add_all", is_flag=True, help="Track every catalog company.")
@click.pass_context
def companies_catalog(ctx: click.Context, add_all: bool) -> None:
    """Count, or track, companies from public new-grad and internship lists.
    \f

    Args:
        ctx: Click invocation context.
        add_all: Whether to track all of them.
    """
    dashboard = _dashboard(ctx)
    catalog = dashboard.get("/api/onboarding/companies/catalog", timeout=_MODEL_TIMEOUT)
    rows = [
        {"slug": row["slug"], "vendor": row["vendor"]} for row in catalog["companies"]
    ]
    result: Json = {"available": len(rows), "source_counts": catalog["source_counts"]}
    if add_all and rows:
        result["added"] = dashboard.send(
            "POST", "/api/companies/bulk", body={"rows": rows}
        )["inserted"]
    _emit(result)


def _probe(dashboard: Dashboard, entries: list[str]) -> list[Json]:
    if not entries:
        return []
    return cast(
        list[Json],
        dashboard.send(
            "POST",
            "/api/companies/probe",
            body={"entries": entries},
            timeout=_MODEL_TIMEOUT,
        )["results"],
    )


# Sources read through the Chrome extension in a browser signed in to them,
# by CLI name -> field on the config's `sources` section.
_EXTENSION_SOURCES = {
    "handshake": "handshake",
    "linkedin": "linkedin_extension",
    "jobright": "jobright",
}
# The extension rewrites a saved http://127.0.0.1:7654 to its own default, so a
# server install points it at the same dashboard through `localhost`.
EXTENSION_DASHBOARD_URL = "http://localhost:7654"


@onboard.group(name="extension")
def extension() -> None:
    """Scan Handshake, LinkedIn or Jobright through signed-in Chrome (optional).

    These sites only show jobs to signed-in users, so Jobfeed reads them
    through its Chrome extension in a Chrome where the user is signed in.
    """


@extension.command(name="status")
@click.pass_context
def extension_status(ctx: click.Context) -> None:
    """Show whether the extension is connected and which of its sources are on.
    \f

    Args:
        ctx: Click invocation context.
    """
    dashboard = _dashboard(ctx)
    _emit(_extension_status(dashboard, dashboard.get("/api/config")))


@extension.command(name="enable")
@click.argument("source", type=click.Choice(sorted(_EXTENSION_SOURCES)))
@click.option(
    "--query",
    "queries",
    multiple=True,
    metavar="TEXT",
    help=(
        "Search text for Handshake or LinkedIn (repeatable). Defaults to the "
        "titles of the enabled LinkedIn and Indeed searches."
    ),
)
@click.pass_context
def extension_enable(ctx: click.Context, source: str, queries: tuple[str, ...]) -> None:
    """Scan SOURCE through the connected extension from now on.

    Needs finished onboarding and a connected extension. Starts no scan.
    \f

    Args:
        ctx: Click invocation context.
        source: Extension source to turn on.
        queries: Search texts; empty means the enabled search titles.

    Raises:
        click.ClickException: If onboarding is unfinished or the extension is
            not connected or cannot scan SOURCE.
        click.UsageError: If no search text is given or enabled.
    """
    dashboard = _dashboard(ctx)
    current = _configured(dashboard)
    bridge = dashboard.get("/api/sources/jobright/status")
    if not bridge.get("connected"):
        raise click.ClickException(
            "The Chrome extension is not connected. Load it in Chrome, set its "
            f"Jobfeed URL to {EXTENSION_DASHBOARD_URL}, and keep Chrome open."
        )
    if source not in bridge.get("supported_sources", []):
        raise click.ClickException(
            f"The connected extension cannot scan {source}. Update the Jobfeed "
            "checkout and reload the extension in chrome://extensions."
        )
    configuration = {key: current[key] for key in _EDITABLE_SECTIONS}
    row = configuration["sources"][_EXTENSION_SOURCES[source]]
    row["enabled"] = True
    if source != "jobright":
        row["queries"] = list(dict.fromkeys(queries)) or _search_titles(dashboard)
    _emit(_extension_status(dashboard, _save(dashboard, configuration)))


@extension.command(name="disable")
@click.argument("source", type=click.Choice(sorted(_EXTENSION_SOURCES)))
@click.pass_context
def extension_disable(ctx: click.Context, source: str) -> None:
    """Stop scanning SOURCE through the extension.
    \f

    Args:
        ctx: Click invocation context.
        source: Extension source to turn off.
    """
    dashboard = _dashboard(ctx)
    current = _configured(dashboard)
    configuration = {key: current[key] for key in _EDITABLE_SECTIONS}
    configuration["sources"][_EXTENSION_SOURCES[source]]["enabled"] = False
    _emit(_extension_status(dashboard, _save(dashboard, configuration)))


def _configured(dashboard: Dashboard) -> Json:
    current = dashboard.get("/api/config")
    if not current.get("configured"):
        raise click.ClickException(
            "Finish onboarding first: run `jobfeed onboard finish`."
        )
    return current


def _save(dashboard: Dashboard, configuration: Json) -> Json:
    return dashboard.send("PUT", "/api/config", body=configuration)


def _search_titles(dashboard: Dashboard) -> list[str]:
    rows = dashboard.get("/api/onboarding/searches").get("searches", [])
    titles = list(dict.fromkeys(row["query"] for row in rows if row["enabled"]))
    if not titles:
        raise click.UsageError("No search is enabled; pass --query TEXT.")
    return titles


def _extension_status(dashboard: Dashboard, configuration: Json) -> Json:
    bridge = dashboard.get("/api/sources/jobright/status")
    sources = configuration["sources"]
    return {
        "connected": bool(bridge.get("connected")),
        "dashboard_url_for_extension": EXTENSION_DASHBOARD_URL,
        "sources": {
            name: {
                "enabled": sources[field]["enabled"],
                **(
                    {"queries": sources[field]["queries"]}
                    if "queries" in sources[field]
                    else {}
                ),
            }
            for name, field in _EXTENSION_SOURCES.items()
        },
    }


@onboard.command(name="check")
@click.pass_context
def check(ctx: click.Context) -> None:
    """Test that setup is complete and every enabled part works. Read-only.

    Prints one line per check and exits 1 when any check fails.
    \f

    Args:
        ctx: Click invocation context.

    Raises:
        SystemExit: With status 1 when a check fails.
    """
    results = setup_checks(_dashboard(ctx))
    _emit({"ok": all(row["ok"] for row in results), "checks": results})
    if not all(row["ok"] for row in results):
        raise SystemExit(1)


def setup_checks(dashboard: Dashboard) -> list[Json]:
    """Return one row per setup check: its name, whether it passed, and why.

    Args:
        dashboard: Client for the running dashboard.

    Returns:
        Rows with ``check``, ``ok`` and ``detail``.
    """
    results: list[Json] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "ok": ok, "detail": detail})

    try:
        health = dashboard.get("/api/health")
    except (httpx.HTTPError, click.ClickException) as exc:
        add("dashboard", False, f"The dashboard does not answer: {exc}")
        return results
    add("dashboard", health.get("status") == "ok", f"database {health.get('db')}")
    state = _status(dashboard)
    missing = [name for name, done in state["steps"].items() if not done]
    add(
        "setup finished",
        not missing,
        "all steps done" if not missing else f"missing: {', '.join(missing)}",
    )
    provider = state["provider"]
    add(
        "AI provider",
        bool(
            provider["connected"]
            and provider["quick_model"]
            and provider["detailed_model"]
        ),
        f"{provider['name']}: quick {provider['quick_model']}, "
        f"detailed {provider['detailed_model']}"
        if provider["name"]
        else "not set up",
    )
    add("résumé", state["resume"] is not None, state["resume"] or "not uploaded")
    searches = len(state["enabled_searches"])
    companies = state["tracked_companies"]
    add(
        "job sources",
        searches + companies > 0,
        f"{searches} searches, {companies} company career pages",
    )
    try:
        active = dashboard.get("/api/runs/active").get("runs", [])
        add("scan API", True, f"{len(active)} runs active")
    except click.ClickException as exc:
        add("scan API", False, str(exc))
    configuration = dashboard.get("/api/config")
    if configuration.get("configured"):
        extension = _extension_status(dashboard, configuration)
        enabled = [name for name, row in extension["sources"].items() if row["enabled"]]
        if enabled:
            add(
                "Chrome extension",
                extension["connected"],
                f"needed for {', '.join(enabled)}; "
                + ("connected" if extension["connected"] else "not connected"),
            )
    return results


@onboard.command(name="finish")
@click.option(
    "--jobs-per-run",
    type=click.IntRange(1, 100_000),
    help="Most new jobs evaluated per run.",
)
@click.option(
    "--daily-cost",
    type=click.FloatRange(0),
    help="Daily model spending limit in USD (API providers).",
)
@click.option(
    "--daily-calls",
    type=click.IntRange(0),
    help="Daily model-call limit.",
)
@click.option(
    "--parallel",
    type=click.IntRange(1),
    help="Evaluations run at the same time.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan without saving it.",
)
@click.pass_context
def finish(  # noqa: PLR0913 - one parameter per Click option
    ctx: click.Context,
    jobs_per_run: int | None,
    daily_cost: float | None,
    daily_calls: int | None,
    parallel: int | None,
    dry_run: bool,
) -> None:
    """Apply the setup with the user's run limits. Starts no scan.

    Omitted limits keep the current values; --dry-run shows them.
    \f

    Args:
        ctx: Click invocation context.
        jobs_per_run: Unique-job evaluation limit per run.
        daily_cost: Daily API spending limit.
        daily_calls: Daily model-call limit.
        parallel: Evaluation concurrency.
        dry_run: Whether to only print the plan.
    """
    dashboard = _dashboard(ctx)
    current = dashboard.get("/api/config")
    configuration = {key: current[key] for key in _EDITABLE_SECTIONS}
    llm = configuration["llm"]
    scoring = configuration["scoring"]
    if jobs_per_run is not None:
        scoring["default_eval_limit"] = jobs_per_run
    if daily_cost is not None:
        llm["max_daily_cost_usd"] = daily_cost
    if daily_calls is not None:
        llm["max_daily_score_calls"] = daily_calls
    if parallel is not None:
        llm["max_concurrent"] = parallel
    plan = {
        "jobs_per_run": scoring["default_eval_limit"],
        "daily_cost_usd": llm["max_daily_cost_usd"],
        "daily_calls": llm["max_daily_score_calls"],
        "parallel": llm["max_concurrent"],
        "source_job_caps": {
            name: configuration["sources"][name]["max_jobs"]
            for name in ("ats", "linkedin_guest", "indeed", "speedyapply")
        },
    }
    if dry_run:
        _emit({"saved": False, "plan": plan})
        return
    saved = dashboard.send(
        "POST",
        "/api/onboarding/finish",
        body={
            "configuration": configuration,
            "expected_jobs": scoring["default_eval_limit"],
        },
    )
    _emit({"saved": bool(saved.get("configured")), "plan": plan})


__all__ = ["DEFAULT_URL", "Dashboard", "onboard", "open_dashboard", "setup_checks"]
