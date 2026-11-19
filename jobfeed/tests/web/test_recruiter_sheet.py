"""The recruiter sheet API: reads, guarded edits and a neutralized CSV."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.config import DBSettings, Settings
from jobfeed.domain.recruiter_sheet import FundingRound
from jobfeed.recruiter_sheet import save_funding
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.recruiter_sheet import router
from tests.support.factories import make_job

OK = 200
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409
INVALID = 422
UNAVAILABLE = 503
PAY = 175000
ORIGIN = {"Origin": "http://127.0.0.1:4654"}
STAGE_B = json.dumps(
    {
        "fit_analysis": {
            "score": 85,
            "strengths": [{"requirement": "@Python", "evidence": "x"}],
            "gaps": [{"requirement": "=Airflow", "severity": "major"}],
        }
    }
)


def evaluate(path: Path, company: str = "=Fixture", title: str = "+Engineer") -> int:
    with closing(sqlite3.connect(path)) as db, db:
        source, real = db.execute("SELECT id,real_job_id FROM jobs").fetchone()
        db.execute(
            "UPDATE jobs SET company=?,title=?,location='New York, NY',jd_text=? "
            "WHERE id=?",
            (company, title, "Pay: $150,000 - $200,000 per year.", source),
        )
        db.execute(
            "INSERT INTO real_job_evaluations(real_job_id,source_job_id,input_jd_text,"
            "input_facts_json,stage_b_verdict,stage_b_json,stage_b_at,updated_at) "
            "VALUES(?,?,'jd','{}','apply',?,'2026-11-06T00:00:00.000000Z','2026')",
            (real, source, STAGE_B),
        )
    return int(real)


@pytest.fixture
async def paths(tmp_path: Path) -> AsyncIterator[tuple[Path, Path]]:
    db = tmp_path / "sheet.db"
    store = SQLiteStore(db)
    await store.connect()
    await store.save_job(make_job())
    await store.close()
    config = tmp_path / "config.toml"
    config.write_text("")
    yield db, config


@pytest.fixture
async def client(paths: tuple[Path, Path]) -> AsyncIterator[httpx.AsyncClient]:
    db, config = paths
    app = FastAPI()
    install_error_handling(app)
    app.state.context = {
        "settings": Settings(db=DBSettings(path=db)),
        "config_path": config,
    }
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4654"
    ) as value:
        yield value


async def test_get_returns_rows_as_stored_with_their_stable_key(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    real = evaluate(paths[0])
    response = await client.get("/api/recruiter-sheet")
    assert response.status_code == OK
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["as_of"]
    [row] = body["rows"]
    assert (row["real_job_id"], row["company"], row["status"], row["fit"]) == (
        real,
        "=Fixture",
        "to_ask",
        85,
    )
    assert row["status_updated_at"] is None
    assert (
        row["pay"],
        row["funding"],
        row["funding_source"],
        row["funding_pending"],
    ) == (PAY, "", None, True)


async def test_get_shows_a_stored_funding_check(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    evaluate(paths[0])
    save_funding(
        paths[0],
        "fixture",
        "=Fixture",
        FundingRound(
            "Series A", 17e6, datetime.now(UTC).date(), "https://example.com/a"
        ),
    )
    [row] = (await client.get("/api/recruiter-sheet")).json()["rows"]
    assert row["funding"].startswith("Series A, $17M, ")
    assert row["funding_source"] == "https://example.com/a"
    assert row["funding_pending"] is False


async def test_csv_neutralizes_cells_and_leads_with_the_stable_key(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    real = evaluate(paths[0])
    response = await client.get("/api/recruiter-sheet.csv")
    assert response.status_code == OK
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert lines[1].startswith(f"{real},'=Fixture,'+Engineer,")
    assert "'@Python" in lines[1]
    assert "'=Airflow" in lines[1]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.invalid"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Origin": "null"},
    ],
)
async def test_writes_are_refused_without_the_dashboard_origin(
    paths: tuple[Path, Path], client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    real = evaluate(paths[0])
    put = await client.put(
        f"/api/recruiter-sheet/{real}", json={"status": "asked"}, headers=headers
    )
    post = await client.post(
        "/api/recruiter-sheet/edits",
        json={"edits": [{"real_job_id": real, "status": "asked"}]},
        headers=headers,
    )
    assert (put.status_code, post.status_code) == (FORBIDDEN, FORBIDDEN)
    [row] = (await client.get("/api/recruiter-sheet")).json()["rows"]
    assert row["status"] == "to_ask"


async def test_put_saves_a_row_and_explains_refusals(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    real = evaluate(paths[0])
    saved = await client.put(
        f"/api/recruiter-sheet/{real}",
        json={"status": "with_recruiter", "recruiter": "Sam", "note": "intro"},
        headers=ORIGIN,
    )
    assert saved.status_code == OK
    assert (saved.json()["status"], saved.json()["recruiter"]) == (
        "with_recruiter",
        "Sam",
    )
    assert saved.json()["status_updated_at"]
    refused = await client.put(
        f"/api/recruiter-sheet/{real}", json={"status": "to_ask"}, headers=ORIGIN
    )
    assert refused.status_code == CONFLICT
    assert refused.json()["error"]["code"] == "invalid_transition"
    missing = await client.put(
        "/api/recruiter-sheet/987654", json={"status": "asked"}, headers=ORIGIN
    )
    assert missing.status_code == NOT_FOUND
    unknown = await client.put(
        f"/api/recruiter-sheet/{real}", json={"status": "nope"}, headers=ORIGIN
    )
    assert unknown.status_code == CONFLICT
    assert unknown.json()["error"]["code"] == "unknown_status"
    extra = await client.put(
        f"/api/recruiter-sheet/{real}",
        json={"status": "asked", "applied": True},
        headers=ORIGIN,
    )
    assert extra.status_code == INVALID


async def test_batch_rejects_per_row_and_saves_the_rest(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    real = evaluate(paths[0])
    response = await client.post(
        "/api/recruiter-sheet/edits",
        json={
            "edits": [
                {"real_job_id": real, "status": "asked", "recruiter": "Sam"},
                {"real_job_id": 987654, "status": "asked"},
                {"real_job_id": real, "status": "not a status"},
            ]
        },
        headers=ORIGIN,
    )
    assert response.status_code == OK
    results = response.json()["results"]
    assert [(r["outcome"], r["code"]) for r in results] == [
        ("applied", None),
        ("rejected", "not_on_sheet"),
        ("rejected", "unknown_status"),
    ]
    assert results[0]["row"]["recruiter"] == "Sam"
    repeat = await client.post(
        "/api/recruiter-sheet/edits",
        json={"edits": [{"real_job_id": real, "status": "asked", "recruiter": "Sam"}]},
        headers=ORIGIN,
    )
    assert repeat.json()["results"][0]["outcome"] == "unchanged"


async def test_batch_size_is_bounded(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/recruiter-sheet/edits",
        json={"edits": [{"real_job_id": 1, "status": "asked"}] * 501},
        headers=ORIGIN,
    )
    assert response.status_code == INVALID


async def test_statuses_read_shows_the_table_and_which_rows_changed(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    real = evaluate(paths[0])
    before = (await client.get("/api/recruiter-sheet/statuses")).json()
    assert before["statuses"] == [
        "to_ask",
        "asked",
        "with_recruiter",
        "passed",
        "apply_myself",
    ]
    assert "to_ask" not in before["transitions"]["with_recruiter"]
    assert before["rows"] == [
        {"real_job_id": real, "status": "to_ask", "status_updated_at": None}
    ]
    await client.put(
        f"/api/recruiter-sheet/{real}", json={"status": "asked"}, headers=ORIGIN
    )
    after = (await client.get("/api/recruiter-sheet/statuses")).json()
    assert after["rows"][0]["status"] == "asked"
    assert after["rows"][0]["status_updated_at"] >= before["as_of"]


async def test_config_changes_apply_on_the_next_read_without_a_restart(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    db, config = paths
    evaluate(db, company="Staffing Co")
    assert len((await client.get("/api/recruiter-sheet")).json()["rows"]) == 1
    config.write_text('[recruiter_sheet]\nexcluded_companies = ["staffing"]\n')
    assert (await client.get("/api/recruiter-sheet")).json()["rows"] == []


async def test_a_broken_config_reports_unavailable(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    paths[1].write_text("[recruiter_sheet]\nunknown = 1\n")
    response = await client.get("/api/recruiter-sheet")
    assert response.status_code == UNAVAILABLE
    assert response.json()["error"]["code"] == "recruiter_sheet_unavailable"


async def test_posting_text_is_not_treated_as_a_link_unless_http(
    paths: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    db = paths[0]
    evaluate(db)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE jobs SET url='javascript:alert(1)'")
    [row] = (await client.get("/api/recruiter-sheet")).json()["rows"]
    assert row["url"] is None
