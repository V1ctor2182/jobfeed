"""The recruiter sheet read from and saved to a real Jobfeed database."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.domain.recruiter_sheet import (
    FundingRecord,
    FundingRound,
    SheetDecision,
    SheetEdit,
    SheetJob,
    SheetPolicy,
    SheetStatus,
    build_sheet,
)
from jobfeed.recruiter_sheet import (
    apply_edits,
    forget_funding,
    funding_questions,
    read_sheet,
    render_csv,
    save_funding,
)
from tests.integration.test_real_job_resolver_sqlite import _parent
from tests.support.sqlite_jobs_evaluations import make_job, open_sqlite_store

PAID = "Base salary: $150,000 - $200,000 per year."
ATS = "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/US/Engineer_R123"
POLICY = SheetPolicy()
TODAY = date(2026, 11, 7)
ROUND = FundingRound("Series A", 17e6, date(2026, 3, 17), "https://example.com/a")


def stage_b(score: int = 85, majors: int = 1) -> str:
    return json.dumps(
        {
            "fit_analysis": {
                "score": score,
                "strengths": [{"requirement": "Python", "evidence": "Alibaba"}],
                "gaps": [
                    {"requirement": f"Gap {i}", "severity": "major"}
                    for i in range(majors)
                ]
                + [{"requirement": "Airflow", "severity": "minor"}],
            }
        }
    )


@dataclass
class Fixture:
    path: Path
    store: SQLiteStore
    lifecycle: object

    async def job(  # noqa: PLR0913
        self,
        name: str,
        *,
        location: str = "New York, NY",
        verdict: str | None = "apply",
        evaluation: str | None = None,
        jd_text: str = PAID,
        company: str = "Example Corp",
    ) -> tuple[int, int]:
        job = replace(
            make_job(name, jd_text=jd_text), location=location, company=company
        )
        saved = await self.store.save_job(job)
        real_job_id = await _parent(self.lifecycle, saved.job_id)  # type: ignore[arg-type]
        source_id = int(saved.job_id)
        self.sql(
            "INSERT INTO real_job_evaluations(real_job_id,source_job_id,input_jd_text,"
            "input_facts_json,stage_b_verdict,stage_b_json,stage_b_at,updated_at) "
            "VALUES(?,?,'jd','{}',?,?,'2026-11-06T00:00:00.000000Z',"
            "'2026-11-06T00:00:00.000000Z')",
            real_job_id,
            source_id,
            verdict,
            evaluation or stage_b(),
        )
        return real_job_id, source_id

    def sql(self, statement: str, *args: object) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(statement, args)

    def queue(self, source_id: int, state: str, owner: str | None = None) -> None:
        self.sql(
            "CREATE TABLE IF NOT EXISTS application_queue("
            "job_id INTEGER UNIQUE,state TEXT,codex_application_session_id TEXT)"
        )
        self.sql("INSERT INTO application_queue VALUES(?,?,?)", source_id, state, owner)


@pytest.fixture
async def sheet(tmp_path: Path) -> AsyncIterator[Fixture]:
    path = tmp_path / "sheet.db"
    lifecycle, store = await open_sqlite_store(path)
    try:
        yield Fixture(path, store, lifecycle)
    finally:
        await lifecycle.close()


async def test_a_new_evaluation_appears_with_no_job_or_timer(sheet: Fixture) -> None:
    assert read_sheet(sheet.path, POLICY) == []
    real_job_id, source_id = await sheet.job("a")
    [row] = read_sheet(sheet.path, POLICY)
    assert (row.real_job_id, row.source_job_ids, row.fit) == (
        real_job_id,
        (source_id,),
        85,
    )
    assert (row.why, row.watch_out) == ("Python", "Gap 0")
    assert row.status is SheetStatus.TO_ASK
    assert row.qualifies


def evaluation_with_gaps(*gaps: tuple[str, str]) -> str:
    return json.dumps(
        {
            "fit_analysis": {
                "score": 85,
                "strengths": [{"requirement": "Python"}],
                "gaps": [{"requirement": r, "severity": s} for r, s in gaps],
            }
        }
    )


async def test_an_older_evaluations_raw_blocker_is_a_critical_gap(
    sheet: Fixture,
) -> None:
    await sheet.job("old", evaluation=evaluation_with_gaps(("Clearance", "blocker")))
    assert read_sheet(sheet.path, POLICY) == []


async def test_an_older_evaluations_raw_notable_counts_and_shows_as_a_major_gap(
    sheet: Fixture,
) -> None:
    await sheet.job(
        "one",
        evaluation=evaluation_with_gaps(("Airflow", "notable"), ("Spark", "minor")),
    )
    [row] = read_sheet(sheet.path, POLICY)
    assert row.watch_out == "Airflow"
    await sheet.job(
        "three",
        evaluation=evaluation_with_gaps(
            ("A", "notable"), ("B", "major"), ("C", "notable")
        ),
    )
    assert [row.watch_out for row in read_sheet(sheet.path, POLICY)] == ["Airflow"]


async def test_rules_read_from_the_database(sheet: Fixture) -> None:
    await sheet.job("low", evaluation=stage_b(score=70))
    await sheet.job("gaps", evaluation=stage_b(majors=3))
    await sheet.job("consider", verdict="consider")
    await sheet.job("buffalo", location="Buffalo, NY, USA")
    await sheet.job("broken", evaluation="not json")
    closed, _ = await sheet.job("closed")
    sheet.sql("UPDATE real_jobs SET official_closed_at='2026-11-01' WHERE id=?", closed)
    ignored, _ = await sheet.job("ignored")
    sheet.sql(
        "UPDATE real_job_status SET status='ignored' WHERE real_job_id=?", ignored
    )
    applied, _ = await sheet.job("applied")
    sheet.sql(
        "INSERT INTO real_job_applications(real_job_id,applied_at) "
        "VALUES(?,'2026-11-01')",
        applied,
    )
    status_applied, _ = await sheet.job("status-applied")
    sheet.sql(
        "UPDATE real_job_status SET status='interviewing' WHERE real_job_id=?",
        status_applied,
    )
    _, legacy_source = await sheet.job("legacy-applied")
    sheet.sql("INSERT INTO applied(job_id) VALUES(?)", legacy_source)
    assert read_sheet(sheet.path, POLICY) == []


async def test_queue_states_decide_as_the_rules_say(sheet: Fixture) -> None:
    queued, queued_source = await sheet.job("queued")
    sheet.queue(queued_source, "Queued")
    _, running = await sheet.job("running")
    sheet.queue(running, "Applying", "claude:1")
    _, unavailable = await sheet.job("unavailable")
    sheet.queue(unavailable, "Unavailable")
    assert [row.real_job_id for row in read_sheet(sheet.path, POLICY)] == [queued]


async def test_batch_edits_reject_per_row_and_apply_the_rest(sheet: Fixture) -> None:
    first, _ = await sheet.job("first")
    second, _ = await sheet.job("second")
    third, _ = await sheet.job("third")
    apply_edits(sheet.path, POLICY, [SheetEdit(third, "with_recruiter", "Sam")])
    results = apply_edits(
        sheet.path,
        POLICY,
        [
            SheetEdit(first, "asked", " Sam ", "intro"),
            SheetEdit(second, "not a status"),
            SheetEdit(third, "to_ask"),
            SheetEdit(987654, "asked"),
        ],
    )
    assert [(r.outcome, r.problem.code if r.problem else None) for r in results] == [
        ("applied", None),
        ("rejected", "unknown_status"),
        ("rejected", "invalid_transition"),
        ("rejected", "not_on_sheet"),
    ]
    rows = {row.real_job_id: row for row in read_sheet(sheet.path, POLICY)}
    assert (rows[first].status, rows[first].recruiter, rows[first].note) == (
        SheetStatus.ASKED,
        "Sam",
        "intro",
    )
    assert rows[second].status is SheetStatus.TO_ASK
    assert rows[third].status is SheetStatus.WITH_RECRUITER
    assert results[0].row == rows[first]
    assert results[3].row is None


async def test_sending_a_whole_sheet_back_changes_nothing(sheet: Fixture) -> None:
    untouched, _ = await sheet.job("untouched")
    edited, _ = await sheet.job("edited")
    [saved] = apply_edits(sheet.path, POLICY, [SheetEdit(edited, "asked", "Sam", "n")])
    before = read_sheet(sheet.path, POLICY)
    results = apply_edits(
        sheet.path,
        POLICY,
        [
            SheetEdit(untouched, "to_ask"),
            SheetEdit(edited, "asked", "Sam", "n"),
        ],
    )
    assert [result.outcome for result in results] == ["unchanged", "unchanged"]
    assert read_sheet(sheet.path, POLICY) == before
    assert saved.row is not None
    assert saved.row.status_updated_at == results[1].row.status_updated_at  # type: ignore[union-attr]
    with closing(sqlite3.connect(sheet.path)) as db:
        stored = [
            row[0] for row in db.execute("SELECT real_job_id FROM recruiter_sheet_rows")
        ]
    assert stored == [edited]


async def test_a_change_moves_the_status_timestamp(sheet: Fixture) -> None:
    job, _ = await sheet.job("a")
    [first] = apply_edits(sheet.path, POLICY, [SheetEdit(job, "asked")])
    [second] = apply_edits(sheet.path, POLICY, [SheetEdit(job, "asked", note="again")])
    assert first.row and second.row
    assert first.row.status_updated_at
    assert second.row.status_updated_at > first.row.status_updated_at
    assert second.row.updated_at == second.row.status_updated_at


async def test_a_stored_row_stays_when_the_job_stops_qualifying(sheet: Fixture) -> None:
    held, held_source = await sheet.job("held")
    left, left_source = await sheet.job("left")
    apply_edits(sheet.path, POLICY, [SheetEdit(held, "with_recruiter", "Sam")])
    for source in (held_source, left_source):
        sheet.queue(source, "Unavailable")
    sheet.sql("UPDATE real_jobs SET official_closed_at='2026-11-01' WHERE id=?", held)
    [row] = read_sheet(sheet.path, POLICY)
    assert (row.real_job_id, row.closed, row.qualifies, row.queue_state) == (
        held,
        True,
        False,
        "Unavailable",
    )
    assert left != held


async def test_an_edit_needs_the_job_on_the_sheet(sheet: Fixture) -> None:
    low, _ = await sheet.job("low", evaluation=stage_b(score=10))
    [result] = apply_edits(sheet.path, POLICY, [SheetEdit(low, "asked")])
    assert result.outcome == "rejected"
    assert result.problem is not None
    assert result.problem.code == "not_on_sheet"
    assert read_sheet(sheet.path, POLICY) == []


async def test_the_decision_follows_the_job_through_an_identity_merge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "merge.db"
    lifecycle, store = await open_sqlite_store(path)
    try:
        a = await store.save_job(replace(make_job("a", jd_text="first"), url=ATS))
        old = replace(make_job("b", jd_text="second"), platform="jobright")
        b = await store.save_job(old)
        parents = [await _parent(lifecycle, job.job_id) for job in (a, b)]
        assert parents[0] != parents[1]
        with closing(sqlite3.connect(path)) as db, db:
            for parent in parents[1:]:
                db.execute(
                    "INSERT INTO recruiter_sheet_rows "
                    "VALUES(?,'with_recruiter','Sam','','2026')",
                    (parent,),
                )
        await store.save_job(replace(old, apply_url=ATS))
        winner = await _parent(lifecycle, a.job_id)
        assert winner == await _parent(lifecycle, b.job_id)
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute(
                "SELECT real_job_id,status,recruiter FROM recruiter_sheet_rows"
            ).fetchall()
        assert rows == [(winner, "with_recruiter", "Sam")]
    finally:
        await lifecycle.close()


async def test_a_database_without_the_table_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE jobs(id INTEGER)")
    with pytest.raises(sqlite3.Error):
        read_sheet(path, POLICY)


async def test_existing_database_gets_the_table_on_connect(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    store = SQLiteStore(path)
    await store.connect()
    await store.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE recruiter_sheet_rows")
    store = SQLiteStore(path)
    await store.connect()
    await store.close()
    assert read_sheet(path, POLICY) == []


async def test_an_omitted_recruiter_or_note_keeps_the_row_value(sheet: Fixture) -> None:
    job, _ = await sheet.job("a")
    apply_edits(sheet.path, POLICY, [SheetEdit(job, "asked", "Sam", "intro")])
    [moved] = apply_edits(sheet.path, POLICY, [SheetEdit(job, "with_recruiter")])
    assert moved.row is not None
    assert (moved.row.status, moved.row.recruiter, moved.row.note) == (
        SheetStatus.WITH_RECRUITER,
        "Sam",
        "intro",
    )
    [cleared] = apply_edits(
        sheet.path, POLICY, [SheetEdit(job, "with_recruiter", note="")]
    )
    assert cleared.row is not None
    assert (cleared.row.recruiter, cleared.row.note) == ("Sam", "")


def test_csv_neutralizes_posting_and_user_text_and_keeps_the_stable_key() -> None:
    hostile = SheetJob(
        real_job_id=7,
        source_job_ids=(70,),
        verdict="apply",
        fit=85,
        company='=HYPERLINK("https://evil.invalid","x")',
        title="+1 Engineer",
        location="New York, NY",
        strengths=("@cmd",),
        major_gaps=("-2 years",),
        url="javascript:alert(1)",
    )
    decision = SheetDecision(SheetStatus.ASKED, "=recruiter", "@note", "2026")
    lines = render_csv(
        build_sheet([hostile], {7: decision}, SheetPolicy(), TODAY)
    ).splitlines()
    assert lines[0].startswith("real_job_id,company,title,location,fit,verdict,why")
    assert lines[1].startswith('7,"\'=HYPERLINK(""https://evil.invalid"",""x"")"')
    for cell in ("'+1 Engineer", "'@cmd", "'-2 years", "'=recruiter", "'@note"):
        assert cell in lines[1]
    assert "javascript" not in lines[1]


def test_csv_adds_pay_and_funding_after_the_existing_columns() -> None:
    record = FundingRecord(
        FundingRound("=Series A", 5e6, date(2026, 3, 17), "https://example.com/a"),
        TODAY,
    )
    funded_job = SheetJob(
        real_job_id=7,
        source_job_ids=(70,),
        company="Acme",
        title="Engineer",
        location="New York, NY",
        url="https://example.com/job",
        verdict="apply",
        fit=85,
        pay=150000,
        funding=record,
    )
    pending = replace(funded_job, real_job_id=8, fit=84, funding=None, pay=140000)
    rows = list(
        csv.DictReader(
            io.StringIO(
                render_csv(build_sheet([funded_job, pending], {}, SheetPolicy(), TODAY))
            )
        )
    )
    assert list(rows[0])[-5:] == [
        "closed",
        "pay",
        "funding",
        "funding_source",
        "funding_pending",
    ]
    assert (
        rows[0]["pay"],
        rows[0]["funding"],
        rows[0]["funding_source"],
        rows[0]["funding_pending"],
    ) == ("150000", "'=Series A, $5M, 2026-03-17", "https://example.com/a", "")
    assert (rows[1]["funding"], rows[1]["funding_pending"]) == ("", "yes")


EARLIER = FundingRound("Seed", 2e6, date(2025, 12, 1), "https://example.com/seed")
OLD_CHECK = "2020-01-01T00:00:00.000000Z"


def age_the_check(sheet: Fixture) -> None:
    sheet.sql("UPDATE company_funding SET checked_at=?", OLD_CHECK)


def checked_at(sheet: Fixture) -> str:
    with closing(sqlite3.connect(sheet.path)) as db:
        return db.execute("SELECT checked_at FROM company_funding").fetchone()[0]


async def test_a_funding_check_is_stored(sheet: Fixture) -> None:
    save_funding(sheet.path, "acme", "Acme, Inc.", ROUND)
    assert funding_rows(sheet) == [
        (
            "acme",
            "Acme, Inc.",
            1,
            "Series A",
            17_000_000.0,
            "2026-03-17",
            "https://example.com/a",
        )
    ]


async def test_finding_no_round_keeps_a_stored_round_and_updates_the_check_time(
    sheet: Fixture,
) -> None:
    save_funding(sheet.path, "acme", "Acme", ROUND)
    age_the_check(sheet)
    before = funding_rows(sheet)
    save_funding(sheet.path, "acme", "Acme Renamed", None)
    assert funding_rows(sheet) == before
    assert checked_at(sheet) > OLD_CHECK


async def test_an_earlier_round_keeps_the_stored_round_and_updates_the_check_time(
    sheet: Fixture,
) -> None:
    save_funding(sheet.path, "acme", "Acme", ROUND)
    age_the_check(sheet)
    before = funding_rows(sheet)
    save_funding(sheet.path, "acme", "Acme", EARLIER)
    assert funding_rows(sheet) == before
    assert checked_at(sheet) > OLD_CHECK


@pytest.mark.parametrize(
    "announced_on", [date(2026, 3, 17), date(2026, 6, 1)], ids=["same_date", "later"]
)
async def test_a_round_of_the_same_date_or_later_replaces_the_stored_round(
    sheet: Fixture, announced_on: date
) -> None:
    save_funding(sheet.path, "acme", "Acme", ROUND)
    newer = FundingRound("Series B", None, announced_on, "https://example.com/b")
    save_funding(sheet.path, "acme", "Acme", newer)
    assert funding_rows(sheet) == [
        (
            "acme",
            "Acme",
            1,
            "Series B",
            None,
            announced_on.isoformat(),
            "https://example.com/b",
        )
    ]


async def test_any_found_round_replaces_a_stored_not_found(sheet: Fixture) -> None:
    save_funding(sheet.path, "acme", "Acme", None)
    assert funding_rows(sheet) == [("acme", "Acme", 0, None, None, None, None)]
    save_funding(sheet.path, "acme", "Acme", EARLIER)
    assert funding_rows(sheet)[0][2:4] == (1, "Seed")


async def test_finding_no_round_again_updates_a_stored_not_found(
    sheet: Fixture,
) -> None:
    save_funding(sheet.path, "acme", "Acme", None)
    age_the_check(sheet)
    save_funding(sheet.path, "acme", "Acme", None)
    assert funding_rows(sheet) == [("acme", "Acme", 0, None, None, None, None)]
    assert checked_at(sheet) > OLD_CHECK


def funding_rows(sheet: Fixture) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(sheet.path)) as db:
        return db.execute(
            "SELECT company_key,company,found,round,amount_usd,announced_on,source_url "
            "FROM company_funding"
        ).fetchall()


async def test_pay_comes_from_the_representative_posting_and_decides_the_rule(
    sheet: Fixture,
) -> None:
    paid, _ = await sheet.job("paid", jd_text="Pay: between $150,000 and $200,000.")
    await sheet.job("low", jd_text="Pay: $90,000 - $110,000 per year.")
    await sheet.job("unstated", jd_text="We pay well.")
    at_floor, _ = await sheet.job("floor", jd_text="Pay: $120,000 - $140,000 per year.")
    rows = {row.real_job_id: row.pay for row in read_sheet(sheet.path, POLICY, TODAY)}
    assert rows == {paid: 175000, at_floor: 130000}


async def test_a_kept_row_still_shows_pay_after_it_stops_qualifying(
    sheet: Fixture,
) -> None:
    real_id, _ = await sheet.job("a")
    apply_edits(sheet.path, POLICY, [SheetEdit(real_id, "asked")], TODAY)
    await sheet.job("other", evaluation=stage_b(score=10))
    sheet.sql("UPDATE real_job_evaluations SET stage_b_verdict='consider'")
    [row] = read_sheet(sheet.path, POLICY, TODAY)
    assert (row.real_job_id, row.qualifies, row.pay) == (real_id, False, 175000)


async def test_a_company_with_no_record_is_pending_until_its_check_decides(
    sheet: Fixture,
) -> None:
    await sheet.job("a", company="Acme, Inc.")
    [row] = read_sheet(sheet.path, POLICY, TODAY)
    assert (row.funding_pending, row.funding, row.funding_source) == (True, "", None)

    save_funding(sheet.path, "acme", "Acme, Inc.", None)
    assert read_sheet(sheet.path, POLICY, TODAY) == []

    save_funding(sheet.path, "acme", "Acme, Inc.", ROUND)
    [row] = read_sheet(sheet.path, POLICY, TODAY)
    assert row.qualifies
    assert (row.funding_pending, row.funding, row.funding_source) == (
        False,
        "Series A, $17M, 2026-03-17",
        "https://example.com/a",
    )


async def test_funding_older_than_the_window_takes_a_job_off_the_sheet(
    sheet: Fixture,
) -> None:
    await sheet.job("a", company="Acme")
    save_funding(sheet.path, "acme", "Acme", ROUND)
    assert len(read_sheet(sheet.path, POLICY, date(2027, 3, 17))) == 1
    assert read_sheet(sheet.path, POLICY, date(2027, 3, 18)) == []


async def test_a_read_refuses_a_database_without_the_table_and_creates_nothing(
    sheet: Fixture,
) -> None:
    await sheet.job("a")
    sheet.sql("DROP TABLE company_funding")
    with pytest.raises(sqlite3.OperationalError):
        read_sheet(sheet.path, POLICY, TODAY)
    with closing(sqlite3.connect(sheet.path)) as db:
        tables = db.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='company_funding'"
        ).fetchone()
    assert tables == (0,)


async def test_funding_questions_name_each_due_company_from_its_best_job(
    sheet: Fixture,
) -> None:
    await sheet.job("b-low", company="Beta", jd_text="B low. Pay: $150,000-$200,000.")
    await sheet.job(
        "b-high",
        company="Beta Inc",
        evaluation=stage_b(score=95),
        jd_text="B high. Pay: $150,000-$200,000.",
    )
    await sheet.job("a", company="Alpha", evaluation=stage_b(score=90))
    await sheet.job("done", company="Done")
    await sheet.job("poor", company="Poor", jd_text="Pay: $50,000-$60,000.")
    save_funding(sheet.path, "done", "Done", ROUND)

    questions = funding_questions(sheet.path, POLICY, datetime.now(UTC).date(), 10)

    assert [(q.company_key, q.company) for q in questions] == [
        ("beta", "Beta Inc"),
        ("alpha", "Alpha"),
    ]
    assert questions[0].jd_text == "B high. Pay: $150,000-$200,000."
    assert [
        q.company_key
        for q in funding_questions(sheet.path, POLICY, datetime.now(UTC).date(), 1)
    ] == ["beta"]


async def test_forgetting_deletes_a_record_and_reports_whether_one_existed(
    sheet: Fixture,
) -> None:
    save_funding(sheet.path, "cerberus capital management", "Cerberus", ROUND)
    save_funding(sheet.path, "acme", "Acme", ROUND)
    assert forget_funding(sheet.path, "cerberus capital management") is True
    assert [row[0] for row in funding_rows(sheet)] == ["acme"]
    assert forget_funding(sheet.path, "cerberus capital management") is False
