"""Observe application batch registration without granting or starting work."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

_MAX_CONFIG_BYTES = 65536


@dataclass(frozen=True)
class BatchConnectionState:
    """Whether a Claude application batch connection is registered, and why not."""

    configured: bool
    detail: str


class _BatchRegistration(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    root: str
    project_root: str
    policy: Literal["jobfeed-existing-resume-batch-v2"]
    jobfeed_db: str
    api_url: str
    project_id: str | None = None
    bootstrap_relay: str | None = None
    codex_db: str | None = None


def loopback_origin(value: str) -> str | None:
    """Normalize a loopback HTTP origin with an explicit port.

    Args:
        value: Origin or URL to check.

    Returns:
        The `http://host:port` origin, or None when the value is not a bare
        loopback HTTP origin.
    """
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return None


def _relay_record_available(config: _BatchRegistration, root: Path) -> bool:
    if not config.codex_db or not config.bootstrap_relay:
        return False
    database = Path(config.codex_db)
    if not database.is_absolute():
        return False
    try:
        with closing(
            sqlite3.connect(
                database.as_uri() + "?mode=ro",
                uri=True,
                timeout=2,
            )
        ) as connection:
            row = connection.execute(
                "SELECT cwd, rollout_path FROM threads WHERE id=?",
                (config.bootstrap_relay,),
            ).fetchone()
        return bool(row and row[0] == str(root) and row[1])
    except (OSError, sqlite3.Error, ValueError):
        return False


def _registration_gaps(
    config: _BatchRegistration,
    root: Path,
    database: Path,
    origin: str,
    project_root: Path,
) -> list[str]:
    gaps = []
    if config.root != str(root) or config.project_root != str(project_root):
        gaps.append("the current application workspace")
    if Path(config.jobfeed_db).resolve() != database.resolve():
        gaps.append("the current job database")
    if loopback_origin(config.api_url) != origin:
        gaps.append("this dashboard address")
    if not config.project_id or not config.project_id.strip():
        gaps.append("a saved Codex project")
    if not config.bootstrap_relay or not config.bootstrap_relay.strip():
        gaps.append("a dedicated application relay chat")
    elif not _relay_record_available(config, project_root):
        gaps.append("the application relay's local Codex record")
    return gaps


def batch_connection_state(
    root: Path, database: Path, origin: str | None, *, project_root: Path | None = None
) -> BatchConnectionState:
    """Compare saved batch registrations with this workspace and dashboard.

    Args:
        root: Pipeline root holding the batch bridge database.
        database: Jobfeed database the registration must use.
        origin: Dashboard origin, or None when unknown.
        project_root: Project root the registration must use; defaults to `root`.

    Returns:
        Whether a registration matches, and the detail shown to the user; an
        unreadable bridge database counts as no registration.
    """
    project_root = (project_root or root).resolve()
    if origin is None:
        return BatchConnectionState(
            False,
            "Open the local dashboard to compare its address with the saved "
            "application batch connection.",
        )
    path = root / ".pipeline/claude-batch-bridge.sqlite3"
    candidates: list[list[str]] = []
    with suppress(OSError, sqlite3.Error, ValueError):
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as connection:
            rows = connection.execute("SELECT config FROM scopes LIMIT 32").fetchall()
        for (raw,) in rows:
            if not isinstance(raw, str) or len(raw) > _MAX_CONFIG_BYTES:
                continue
            try:
                config = _BatchRegistration.model_validate(json.loads(raw))
            except (ValueError, TypeError):
                continue
            candidates.append(
                _registration_gaps(
                    config, root.resolve(), database, origin, project_root
                )
            )
    if not candidates:
        return BatchConnectionState(
            False,
            "Register a Claude application batch connection for this workspace "
            "using existing resume PDFs. Registration does not start applications.",
        )
    missing = min(candidates, key=len)
    if missing:
        return BatchConnectionState(
            False,
            "The saved application batch connection still needs "
            + ", ".join(missing)
            + ". Keep this separate from the diagnostic relay. "
            "Registering a connection does not authorize applications.",
        )
    return BatchConnectionState(
        True,
        "Saved batch registration matches this workspace, database and "
        "dashboard, with a project and relay record. Live Claude and relay "
        "connectivity remain unverified. Each application plan still needs "
        "separate authorization.",
    )
