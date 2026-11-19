"""Batch registration evidence is separate from diagnostic readiness."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobfeed.onboarding_batch_setup import batch_connection_state, loopback_origin


def _registration(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "pipeline"
    (root / ".pipeline").mkdir(parents=True)
    jobfeed = tmp_path / "jobfeed.sqlite"
    codex = tmp_path / "codex.sqlite"
    with sqlite3.connect(codex) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            (
                "application-relay",
                str(root),
                "private-rollout-path",
            ),
        )
    config = {
        "root": str(root),
        "project_root": str(root),
        "policy": "jobfeed-existing-resume-batch-v2",
        "jobfeed_db": str(jobfeed),
        "api_url": "http://127.0.0.1:8765",
        "project_id": "private-project-id",
        "bootstrap_relay": "application-relay",
        "codex_db": str(codex),
    }
    return root, jobfeed, config


def _save(root: Path, config: dict[str, object]) -> Path:
    path = root / ".pipeline/claude-batch-bridge.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE scopes (scope TEXT, config TEXT)")
        connection.execute("CREATE TABLE grants (plan TEXT)")
        connection.execute("CREATE TABLE actions (id TEXT)")
        connection.execute(
            "INSERT INTO scopes VALUES (?, ?)", ("existing-resumes", json.dumps(config))
        )
    return path


def test_matching_registration_does_not_claim_live_connectivity_or_grant(
    tmp_path: Path,
) -> None:
    root, database, config = _registration(tmp_path)
    path = _save(root, config)
    before = path.read_bytes()
    result = batch_connection_state(root, database, "http://127.0.0.1:8765")
    assert result.configured is True
    assert "connectivity remain unverified" in result.detail
    assert "separate authorization" in result.detail
    assert "private-project-id" not in result.detail
    assert str(tmp_path) not in result.detail
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_legacy_dashboard_port_and_missing_registration_are_actionable(
    tmp_path: Path,
) -> None:
    root, database, config = _registration(tmp_path)
    config.update(
        api_url="http://127.0.0.1:7654", project_id=None, bootstrap_relay=None
    )
    _save(root, config)
    result = batch_connection_state(root, database, "http://127.0.0.1:8765")
    assert result.configured is False
    assert "this dashboard address" in result.detail
    assert "a saved Codex project" in result.detail
    assert "a dedicated application relay chat" in result.detail


@pytest.mark.parametrize(
    "field", ["jobfeed_db", "root", "project_root", "bootstrap_relay"]
)
def test_foreign_database_workspace_or_missing_relay_record_is_not_configured(
    tmp_path: Path, field: str
) -> None:
    root, database, config = _registration(tmp_path)
    config[field] = str(tmp_path / "foreign")
    _save(root, config)
    assert (
        batch_connection_state(root, database, "http://127.0.0.1:8765").configured
        is False
    )


def test_missing_batch_store_is_observed_without_creating_it(tmp_path: Path) -> None:
    root, database, _config = _registration(tmp_path)
    result = batch_connection_state(root, database, "http://127.0.0.1:8765")
    assert result.configured is False
    assert "existing resume PDFs" in result.detail
    assert not (root / ".pipeline/claude-batch-bridge.sqlite3").exists()


def test_combined_project_binding_keeps_runtime_separate(tmp_path: Path) -> None:
    root, database, config = _registration(tmp_path)
    config["project_root"] = str(tmp_path)
    with sqlite3.connect(str(config["codex_db"])) as connection:
        connection.execute("UPDATE threads SET cwd=?", (str(tmp_path),))
    _save(root, config)
    result = batch_connection_state(
        root, database, "http://127.0.0.1:8765", project_root=tmp_path
    )
    assert result.configured is True


def test_combined_binding_rejects_relay_in_nested_runtime(tmp_path: Path) -> None:
    root, database, config = _registration(tmp_path)
    config["project_root"] = str(tmp_path)
    _save(root, config)
    result = batch_connection_state(
        root, database, "http://127.0.0.1:8765", project_root=tmp_path
    )
    assert result.configured is False
    assert "relay's local Codex record" in result.detail


def test_legacy_policy_does_not_count_as_existing_pdf_setup(tmp_path: Path) -> None:
    root, database, config = _registration(tmp_path)
    config["policy"] = "legacy-resume-generation"
    _save(root, config)
    assert (
        batch_connection_state(root, database, "http://127.0.0.1:8765").configured
        is False
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "http://127.0.0.1:8765/path",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1",
        "http://localhost:bad",
        "http://localhost:8765?query=x",
    ],
)
def test_only_explicit_loopback_dashboard_origins_are_accepted(origin: str) -> None:
    assert loopback_origin(origin) is None


def test_dashboard_origin_preserves_actual_port() -> None:
    assert loopback_origin("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
