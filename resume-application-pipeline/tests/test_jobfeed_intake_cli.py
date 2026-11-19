import asyncio
import json
import os
import subprocess
import sys

import pytest

from test_jobfeed_intake import ROOT, URL, import_fixture, rows, write
from test_jobfeed_intake import db as db
from test_jobfeed_intake import fixture_python as fixture_python


def invoke(*arguments, stdin=None, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "pipeline.jobfeed_intake_cli", *map(str, arguments)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("JOBFEED_")},
            "PYTHONPATH": str(ROOT / "resume-application-pipeline"),
        },
        timeout=30,
    )


def test_cli_prioritizes_duplicates_and_preserves_terminal_state(db, fixture_python):
    arguments = ["--db", db, "--jobfeed-python", fixture_python]
    urls = [URL, URL + "?utm_source=claude"]
    first = invoke(*arguments, "--file", "-", stdin=json.dumps(urls))
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    assert result["counts_by_status"] == {"queued": 1, "already_queued": 1}
    assert result["newly_queued"] == 1
    assert [item["index"] for item in result["results"]] == [0, 1]
    assert [item["url"] for item in result["results"]] == urls
    assert rows(db, "SELECT manual_priority,state FROM application_queue") == [(1, "Queued")]
    write(db, "UPDATE application_queue SET state='Cancelled',manual_priority=0")
    repeated = invoke(*arguments, URL)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["counts_by_status"] == {"already_queued": 1}
    assert rows(db, "SELECT manual_priority,state,codex_application_session_id,submit_authorization FROM application_queue") == [
        (1, "Cancelled", None, None)
    ]
    assert len((fixture_python.parent / "fetches.txt").read_text().splitlines()) == 1


@pytest.mark.parametrize("payload", [
    "not JSON", "{}", "[]", '[42]', json.dumps([URL] * 21),
    json.dumps([URL, "company name"]), json.dumps([URL, "file:///private/file"]),
    json.dumps([URL, "https://example.com/job with spaces"]),
])
def test_cli_rejects_entire_invalid_input_before_fetch_or_write(db, fixture_python, payload):
    result = invoke("--db", db, "--jobfeed-python", fixture_python, "--file", "-", stdin=payload)
    assert result.returncode == 2
    assert result.stdout == ""
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]
    assert not (fixture_python.parent / "fetches.txt").exists()


def test_cli_resolves_config_relative_database_independent_of_cwd(db, fixture_python, tmp_path):
    configured_root = tmp_path / "jobfeed"
    configured_root.mkdir()
    (configured_root / "src").symlink_to(ROOT / "jobfeed" / "src", target_is_directory=True)
    (configured_root / "config.toml").write_text('[db]\npath = "../jobs.sqlite"\n')
    source = tmp_path / "urls.json"
    source.write_text(json.dumps([URL]))
    result = invoke(
        "--jobfeed-root", configured_root, "--jobfeed-python", fixture_python,
        "--file", source, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["newly_queued"] == 1
    assert rows(db, "SELECT manual_priority FROM application_queue") == [(1,)]


def test_cli_help_does_not_require_config_or_database(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = invoke("--jobfeed-root", missing, "--db", missing / "jobs.sqlite", "--help", cwd=tmp_path)
    assert result.returncode == 0
    assert "without starting applications" in result.stdout
    assert not missing.exists()


def test_cli_refuses_missing_database_without_creating_it(tmp_path):
    missing = tmp_path / "does-not-exist.sqlite"
    result = invoke("--db", missing, URL)
    assert result.returncode == 2
    assert not missing.exists()


def test_cli_refuses_ambiguous_input_sources(db, tmp_path):
    source = tmp_path / "urls.json"
    source.write_text(json.dumps([URL]))
    result = invoke("--db", db, "--file", source, URL)
    assert result.returncode == 2
    assert rows(db, "SELECT count(*) FROM jobs") == [(0,)]


def test_cli_reports_partial_outcomes_and_keeps_successes(db, fixture_python):
    asyncio.run(import_fixture(db))
    write(db, "UPDATE jobs SET closed_at='2026-10-21T12:00:00Z'")
    result = invoke("--db", db, "--jobfeed-python", fixture_python, URL, URL.replace("/123", "/124"))
    assert result.returncode == 1, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["counts_by_status"] == {"closed": 1, "queued": 1}
    assert outcome["newly_queued"] == 1
    assert rows(db, "SELECT manual_priority,state FROM application_queue") == [(1, "Queued")]
