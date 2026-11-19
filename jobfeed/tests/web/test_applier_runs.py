"""Applier runs and traces are read from pipeline records without changing them."""

# ruff: noqa: PLR2004, RUF001

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from jobfeed.applier_trace import ApplierTraceReader, encode_project_dir
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import FakeStore, fake_context, open_client

T0 = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
CWD = "/home/someone/pipeline/applications/existing-abc-7/claude-applier"
SESSION = "9df5f433-6da8-4b51-b84b-3c40df6eddd3"

# Copied from pipeline/applier_runtime.py and pipeline/claude_applier.py.
RUNTIME_SCHEMA = """
CREATE TABLE applier_jobs(
 job_id INTEGER PRIMARY KEY, owner TEXT NOT NULL UNIQUE, scope TEXT NOT NULL, model TEXT NOT NULL,
 created_at TEXT NOT NULL, last_progress_at TEXT, last_progress TEXT);
CREATE TABLE applier_requests(
 id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
 blocker_type TEXT, field TEXT, text TEXT NOT NULL, evidence TEXT, state TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE applier_replies(
 request_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE, sender_id TEXT NOT NULL,
 received_at TEXT NOT NULL, PRIMARY KEY(request_id,version));
CREATE TABLE applier_submits(
 job_id INTEGER PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL, requested_at TEXT NOT NULL,
 outcome_at TEXT, evidence TEXT);
CREATE TABLE applier_launches(
 launch_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
 background_id TEXT, session_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""  # noqa: E501

SECRETS = (
    "SECRET-FORM-VALUE",
    "SECRET-PAGE-TEXT",
    "SECRET-REPLY",
    "SECRET-TYPED",
    "SECRET-SCRIPT",
    "SECRET-QUERY",
    "SECRET-ASK-INPUT",
)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def transcript_at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Transcript:
    """Claude transcript lines in the shape the CLI writes them."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = [{"type": "custom-title", "title": "x"}]

    def call(self, call_id: str, name: str, tool_input: dict[str, Any], t: float):
        self.lines.append(
            {
                "type": "assistant",
                "timestamp": transcript_at(t),
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": tool_input,
                        }
                    ]
                },
            }
        )
        # The CLI also writes untimed request copies of each call.
        self.lines.append(
            {
                "type": "api-request-blob",
                "message": {
                    "content": [{"type": "tool_use", "id": call_id, "name": name}]
                },
            }
        )
        return self

    def result(self, call_id: str, t: float, content: str, *, error: bool = False):
        block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
        if error:
            block["is_error"] = True
        self.lines.append(
            {
                "type": "user",
                "timestamp": transcript_at(t),
                "message": {"content": [block]},
            }
        )
        return self

    def write(self, projects: Path, session_id: str) -> None:
        directory = projects / encode_project_dir(CWD)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in self.lines)
        )


def make_runtime(  # noqa: PLR0913
    root: Path,
    *,
    job_id: int = 7,
    state: str = "running",
    session_id: str | None = SESSION,
    created: float = 1,
    updated: float = 30,
) -> sqlite3.Connection:
    path = root / ".pipeline" / "applier-runtime.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(RUNTIME_SCHEMA)
    add_launch(db, job_id, state, session_id, created, updated)
    return db


def add_launch(  # noqa: PLR0913
    db: sqlite3.Connection,
    job_id: int,
    state: str,
    session_id: str | None,
    created: float,
    updated: float,
) -> None:
    db.execute(
        "INSERT INTO applier_jobs(job_id,owner,scope,model,created_at) "
        "VALUES(?,?,?,?,?)",
        (job_id, f"owner-{job_id}", "scope", "sonnet", at(created - 1)),
    )
    db.execute(
        "INSERT INTO applier_launches VALUES(?,?,?,?,?,?,?,?,?)",
        (
            f"launch-{job_id}",
            job_id,
            f"applier-{job_id}",
            json.dumps({"cwd": CWD, "argv": ["claude", "SECRET-SCRIPT"]}),
            None,
            session_id,
            state,
            at(created),
            at(updated),
        ),
    )
    db.commit()


class LabeledStore(FakeStore):
    async def get_job(self, job_id: str) -> SimpleNamespace | None:
        if job_id == "7":
            return SimpleNamespace(company="Valon", title="Software Engineer")
        return None


def make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: float = 90
) -> FastAPI:
    root = tmp_path / "pipeline"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(root))
    app = build_web_app(fake_context(LabeledStore()), static_dir=tmp_path / "no-dist")
    app.state.applier_traces = ApplierTraceReader(
        root,
        projects_dir=tmp_path / "projects",
        now=lambda: T0 + timedelta(seconds=now),
    )
    return app


def tree(path: Path) -> dict[str, tuple[int, int]]:
    return {
        str(item.relative_to(path)): (item.stat().st_size, item.stat().st_mtime_ns)
        for item in sorted(path.rglob("*"))
    }


def steps(node: dict[str, Any]) -> list[dict[str, Any]]:
    if node["kind"] == "step":
        return [node]
    return [step for child in node["children"] for step in steps(child)]


def full_run() -> Transcript:
    return (
        Transcript()
        .call("t1", "ToolSearch", {"query": "SECRET-QUERY"}, 5)
        .result("t1", 5.5, "tools")
        .call(
            "t2",
            "mcp__claude-in-chrome__navigate",
            {"url": "https://jobs.example.com/apply?q=SECRET-QUERY", "tabId": 1},
            8,
        )
        .result("t2", 9, "SECRET-PAGE-TEXT")
        .call(
            "t3",
            "mcp__claude-in-chrome__form_input",
            {"value": "SECRET-FORM-VALUE"},
            10,
        )
        .result("t3", 11, "ok")
        .call(
            "t4",
            "mcp__claude-in-chrome__form_input",
            {"value": "SECRET-FORM-VALUE"},
            11.5,
        )
        .result("t4", 12, "ok")
        .call(
            "t5",
            "mcp__claude-in-chrome__form_input",
            {"value": "SECRET-FORM-VALUE"},
            12.5,
        )
        .result("t5", 13, "ok")
        .call(
            "t6",
            "mcp__claude-in-chrome__form_input",
            {"value": "SECRET-FORM-VALUE"},
            14,
        )
        .result("t6", 15, "SECRET-PAGE-TEXT no such element", error=True)
        .call(
            "t7",
            "mcp__claude-in-chrome__form_input",
            {"value": "SECRET-FORM-VALUE"},
            16,
        )
        .result("t7", 17, "ok")
        .call("t8", "mcp__applier__ask_user", {"question": "SECRET-ASK-INPUT"}, 20)
        .result("t8", 50, "SECRET-REPLY")
        .call(
            "t9",
            "mcp__claude-in-chrome__javascript_tool",
            {"text": "SECRET-SCRIPT"},
            55,
        )
        .result("t9", 56, "SECRET-PAGE-TEXT")
        .call(
            "t10",
            "mcp__claude-in-chrome__computer",
            {"action": "type", "text": "SECRET-TYPED"},
            60,
        )
    )


async def test_missing_runtime_store_lists_no_runs_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch)
    before = tree(tmp_path)
    async with open_client(app) as client:
        runs = await client.get("/api/applier-runs")
        trace = await client.get("/api/applier-runs/7/trace")
    assert runs.status_code == 200
    assert runs.json() == {"runs": [], "autopilot_enabled": False}
    assert trace.status_code == 404
    assert trace.json()["error"]["code"] == "applier_run_not_found"
    assert tree(tmp_path) == before


CRASHED_WRITER = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=1")
db.execute("BEGIN")
db.execute("UPDATE applier_launches SET state='crashed'")
db.execute("CREATE TABLE junk(x)")
db.execute("INSERT INTO junk WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<300) SELECT randomblob(2000) FROM c")
os._exit(1)
"""  # noqa: E501


async def test_a_write_cut_off_by_a_crash_is_rolled_back_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch)
    make_runtime(tmp_path / "pipeline").close()
    path = tmp_path / "pipeline" / ".pipeline" / "applier-runtime.sqlite3"
    subprocess.run([sys.executable, "-c", CRASHED_WRITER, str(path)], check=False)
    assert path.with_name(path.name + "-journal").exists()
    async with open_client(app) as client:
        response = await client.get("/api/applier-runs")
    assert response.status_code == 200
    assert [(run["job_id"], run["state"]) for run in response.json()["runs"]] == [
        (7, "running")
    ]
    assert not path.with_name(path.name + "-journal").exists()


async def test_runs_list_newest_first_with_job_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch)
    db = make_runtime(tmp_path / "pipeline", state="finished")
    add_launch(db, 8, "running", None, created=40, updated=45)
    full_run().write(tmp_path / "projects", SESSION)
    before = tree(tmp_path)
    async with open_client(app) as client:
        response = await client.get("/api/applier-runs")
    assert [run["job_id"] for run in response.json()["runs"]] == [8, 7]
    newest, older = response.json()["runs"]
    assert newest["company"] is None
    assert newest["is_live"] is True
    assert older == {
        "job_id": 7,
        "company": "Valon",
        "title": "Software Engineer",
        "state": "finished",
        "started_at": at(0),
        "updated_at": at(30),
        "is_live": False,
        "session_count": 1,
    }
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ('{"enabled": true}', True),
        ('{"enabled": false}', False),
        ("not json", False),
        (None, False),
    ],
)
async def test_runs_list_reports_the_autopilot_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str | None,
    expected: bool,
) -> None:
    app = make_app(tmp_path, monkeypatch)
    db = make_runtime(tmp_path / "pipeline")
    db.execute(
        "CREATE TABLE applier_control("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    if record is not None:
        db.execute(
            "INSERT INTO applier_control VALUES('autopilot', ?, ?)", (record, at(0))
        )
    db.commit()
    before = tree(tmp_path)
    async with open_client(app) as client:
        response = await client.get("/api/applier-runs")
    assert response.json()["autopilot_enabled"] is expected
    assert tree(tmp_path) == before


async def test_live_trace_merges_calls_isolates_failures_and_shows_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch, now=90)
    db = make_runtime(tmp_path / "pipeline")
    db.execute(
        "INSERT INTO applier_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "r1",
            7,
            "question",
            "f",
            None,
            "Salary",
            "What salary do you expect?",
            None,
            "answered",
            at(20.5),
        ),
    )
    db.execute(
        "INSERT INTO applier_replies "
        "VALUES('r1',1,'SECRET-REPLY','discord','m1','u',?)",
        (at(49),),
    )
    db.commit()
    full_run().write(tmp_path / "projects", SESSION)
    async with open_client(app) as client:
        response = await client.get("/api/applier-runs/7/trace")
    assert response.status_code == 200
    trace = response.json()
    for secret in SECRETS:
        assert secret not in response.text
    assert trace["is_live"] is True
    assert trace["fieldsTotal"] is None
    assert trace["subtitle"].startswith("Software Engineer at Valon")
    worker = trace["root"]
    assert worker["end"] == 90
    assert [child["name"] for child in worker["children"]] == [
        "Launch Applier",
        "Applier",
    ]
    launch, applier = worker["children"]
    assert (launch["start"], launch["end"]) == (1, 5)
    assert applier["meta"] == "Claude CLI · 9df5f433 · sonnet"
    assert [(s["name"], s["meta"], s["outcome"]) for s in applier["children"]] == [
        ("Load tools", "ToolSearch", "ok"),
        ("Open page", "navigate · jobs.example.com", "ok"),
        ("Fill field", "form_input × 3", "ok"),
        ("Fill field", "form_input", "failed"),
        ("Fill field", "form_input", "ok"),
        ("Ask you", "Salary", "ok"),
        ("Run script", "javascript_tool", "ok"),
        ("Click or type", "computer · type", "ok"),
    ]
    merged, failed, retry, wait = applier["children"][2:6]
    assert (merged["start"], merged["end"], merged["fieldsFilled"]) == (10, 13, 3)
    assert failed["fieldsFilled"] == 0
    assert retry["fieldsFilled"] == 1
    assert (wait["category"], wait["start"], wait["end"]) == ("human", 20.5, 49)
    assert wait["note"] == "What salary do you expect?"
    running = applier["children"][-1]
    assert (running["start"], running["end"]) == (60, 90)


async def test_open_request_and_submit_follow_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch, now=120)
    db = make_runtime(tmp_path / "pipeline")
    db.execute(
        "INSERT INTO applier_requests VALUES('r1',7,'blocker','f','missing_fact',"
        "NULL,?,NULL,'open',?)",
        ("x" * 500, at(100)),
    )
    db.execute(
        "INSERT INTO applier_submits VALUES(7,'not_submitted',1,?,?,NULL)",
        (at(70), at(72)),
    )
    db.commit()
    full_run().write(tmp_path / "projects", SESSION)
    async with open_client(app) as client:
        trace = (await client.get("/api/applier-runs/7/trace")).json()
    by_name = {step["name"]: step for step in steps(trace["root"])}
    ask = by_name["Ask you"]
    assert (ask["meta"], ask["start"], ask["end"]) == ("missing_fact", 100, 120)
    assert len(ask["note"]) == 200
    submit = by_name["Submit"]
    assert (submit["start"], submit["end"], submit["outcome"]) == (70, 72, "failed")


async def test_a_draft_sent_back_for_changes_still_shows_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch, now=120)
    db = make_runtime(tmp_path / "pipeline")
    db.execute(
        "INSERT INTO applier_requests VALUES('r1',7,'question','f',NULL,"
        "'Why Valon','Why Valon?',NULL,'redrafted',?)",
        (at(30),),
    )
    db.execute(
        "INSERT INTO applier_replies VALUES('r1',1,'Shorter','discord','m1','u',?)",
        (at(45),),
    )
    db.commit()
    full_run().write(tmp_path / "projects", SESSION)
    async with open_client(app) as client:
        trace = (await client.get("/api/applier-runs/7/trace")).json()
    ask = {step["name"]: step for step in steps(trace["root"])}["Ask you"]
    assert (ask["meta"], ask["start"], ask["end"]) == ("Why Valon", 30, 45)


async def test_without_a_recorded_session_every_later_transcript_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path, monkeypatch)
    make_runtime(tmp_path / "pipeline", session_id=None, state="stopped", updated=80)
    projects = tmp_path / "projects"
    Transcript().call("old", "Read", {"file_path": "/a/notes.md"}, -600).write(
        projects, "older-session"
    )
    Transcript().call("a", "Read", {"file_path": "/a/b/profile.md"}, 5).result(
        "a", 6, "SECRET-PAGE-TEXT"
    ).call(
        "c", "mcp__applier__record_progress", {"step": "Filled SECRET-FORM-VALUE"}, 7
    ).result("c", 8, "ok").write(projects, "first-session")
    Transcript().call(
        "b", "mcp__applier__record_progress", {"step": "ready for review"}, 40
    ).write(projects, "restart-session")
    async with open_client(app) as client:
        runs = (await client.get("/api/applier-runs")).json()["runs"]
        response = await client.get("/api/applier-runs/7/trace")
    assert runs[0]["session_count"] == 2
    trace = response.json()
    assert "SECRET-PAGE-TEXT" not in response.text
    assert "SECRET-FORM-VALUE" not in response.text
    appliers = [c for c in trace["root"]["children"] if c["kind"] == "group"]
    assert [group["meta"] for group in appliers] == [
        "Claude CLI · first-se · sonnet",
        "Claude CLI · restart- · sonnet",
    ]
    assert [step["meta"] for step in appliers[0]["children"]] == [
        "Read · profile.md",
        "record_progress",
    ]
    unfinished = appliers[1]["children"][0]
    assert unfinished["meta"] == "record_progress · ready for review"
    assert unfinished["end"] == 80
    assert trace["is_live"] is False
    assert trace["root"]["end"] == 80
