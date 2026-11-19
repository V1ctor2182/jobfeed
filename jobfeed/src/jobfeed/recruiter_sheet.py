"""Read the recruiter sheet from SQLite and save the user's edits to it.

The dashboard routes, a sheet sync and the pipeline's autopilot hold all read through
here, with no service in between, so they see the same rows.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from jobfeed.domain.company_names import normalize_company_name
from jobfeed.domain.job_priority import compensation_midpoint
from jobfeed.domain.recruiter_sheet import (
    EditProblem,
    FundingRecord,
    FundingRound,
    QueueEntry,
    SheetDecision,
    SheetEdit,
    SheetJob,
    SheetPolicy,
    SheetRow,
    SheetStatus,
    build_sheet,
    edit_problem,
    funding_lookups_needed,
    needs_pay,
    neutralize_cell,
    parse_announced_on,
)
from jobfeed.domain.scoring_parse import normalize_severity
from jobfeed.domain.user_decisions import decision_for_status

_BUSY_SECONDS = 5
_SHEET_JOBS = (
    "SELECT r.id FROM real_jobs r JOIN real_job_evaluations e ON e.real_job_id=r.id "
    "WHERE e.stage_b_verdict='apply' "
    "UNION SELECT real_job_id FROM recruiter_sheet_rows"
)
_CSV_COLUMNS = (
    "real_job_id",
    "company",
    "title",
    "location",
    "fit",
    "verdict",
    "why",
    "watch_out",
    "url",
    "status",
    "recruiter",
    "note",
    "updated_at",
    "closed",
    "pay",
    "funding",
    "funding_source",
    "funding_pending",
)
_NOT_ON_SHEET = EditProblem("not_on_sheet", "This job is not on the recruiter sheet.")


@dataclass(frozen=True)
class EditResult:
    """What became of one edit. A rejected edit leaves the row as it was."""

    real_job_id: int
    outcome: str
    problem: EditProblem | None = None
    row: SheetRow | None = None


@dataclass(frozen=True)
class FundingQuestion:
    """What a funding check needs to know about one company."""

    company_key: str
    company: str
    title: str
    jd_text: str


def _texts(items: object, key: str) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    values = [item.get(key) for item in items if isinstance(item, dict)]
    return tuple(value for value in values if isinstance(value, str) and value.strip())


def _severity(gap: object) -> str | None:
    raw = gap.get("severity") if isinstance(gap, dict) else None
    return normalize_severity(raw) if isinstance(raw, str) else None


def _evaluation(text: str | None) -> dict[str, Any]:
    """Read Stage B output. An unparsable row has no fit, so it cannot qualify."""
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    analysis = data.get("fit_analysis") if isinstance(data, dict) else None
    if not isinstance(analysis, dict):
        return {}
    gaps = analysis.get("gaps")
    gaps = gaps if isinstance(gaps, list) else []
    score = analysis.get("score")
    return {
        "fit": score if type(score) is int else None,
        "critical_gaps": sum(_severity(gap) == "critical" for gap in gaps),
        "major_gaps": _texts(
            [gap for gap in gaps if _severity(gap) == "major"], "requirement"
        ),
        "strengths": _texts(
            analysis.get("strengths") or analysis.get("strong_match"), "requirement"
        ),
    }


def _sources(
    db: sqlite3.Connection,
) -> tuple[
    Mapping[int, tuple[int, ...]],
    Mapping[int, tuple[QueueEntry, ...]],
    set[int],
]:
    """Read each source posting of the sheet's jobs, with queue row and applied mark."""
    has_queue = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_queue'"
    ).fetchone()
    queue = (
        "(SELECT state FROM application_queue q WHERE q.job_id=j.id),"
        "(SELECT codex_application_session_id FROM application_queue q "
        "WHERE q.job_id=j.id)"
        if has_queue
        else "NULL,NULL"
    )
    rows = db.execute(
        f"SELECT j.real_job_id,j.id,{queue},"
        "EXISTS(SELECT 1 FROM applied a WHERE a.job_id=j.id) "
        f"FROM jobs j WHERE j.real_job_id IN ({_SHEET_JOBS}) ORDER BY j.id"
    ).fetchall()
    sources: dict[int, list[int]] = defaultdict(list)
    entries: dict[int, list[QueueEntry]] = defaultdict(list)
    applied: set[int] = set()
    for real_id, source_id, state, owner, was_applied in rows:
        real_job_id = int(real_id)
        sources[real_job_id].append(int(source_id))
        if state is not None:
            entries[real_job_id].append(QueueEntry(state, owner))
        if was_applied:
            applied.add(real_job_id)
    return (
        {key: tuple(value) for key, value in sources.items()},
        {key: tuple(value) for key, value in entries.items()},
        applied,
    )


def _funding(db: sqlite3.Connection) -> dict[str, FundingRecord]:
    return {
        row[0]: FundingRecord(
            FundingRound(row[2], row[3], parse_announced_on(row[4]), row[5])
            if row[1]
            else None,
            date.fromisoformat(row[6][:10]),
        )
        for row in db.execute(
            "SELECT company_key,found,round,amount_usd,announced_on,source_url,"
            "checked_at FROM company_funding"
        )
    }


def _with_pay(
    db: sqlite3.Connection,
    jobs: list[SheetJob],
    decisions: Mapping[int, SheetDecision],
    policy: SheetPolicy,
    today: date,
) -> list[SheetJob]:
    """Read pay only for jobs that could show it, since autopilot reads every second."""
    wanted = [
        job.real_job_id
        for job in jobs
        if job.real_job_id in decisions or needs_pay(job, policy, today)
    ]
    pay = {
        int(real_id): compensation_midpoint(text or "")
        for real_id, text in db.execute(
            "SELECT r.id,j.jd_text FROM real_jobs r "
            "JOIN jobs j ON j.id=r.representative_job_id "
            f"WHERE r.id IN ({','.join('?' * len(wanted))})",
            wanted,
        )
    }
    return [replace(job, pay=pay.get(job.real_job_id)) for job in jobs]


def _jobs(
    db: sqlite3.Connection,
    decisions: Mapping[int, SheetDecision],
    policy: SheetPolicy,
    today: date,
) -> list[SheetJob]:
    sources, entries, applied = _sources(db)
    funding = _funding(db)
    applied.update(
        int(row[0])
        for row in db.execute("SELECT real_job_id FROM real_job_applications")
    )
    rows = db.execute(
        "SELECT r.id,r.official_closed_at,j.company,j.title,j.location,j.url,"
        "j.closed_at,e.stage_b_verdict,e.stage_b_json,e.stage_b_at,s.status "
        "FROM real_jobs r JOIN jobs j ON j.id=r.representative_job_id "
        "LEFT JOIN real_job_evaluations e ON e.real_job_id=r.id "
        "LEFT JOIN real_job_status s ON s.real_job_id=r.id "
        f"WHERE r.id IN ({_SHEET_JOBS})"
    ).fetchall()
    jobs = []
    for row in rows:
        real_job_id = int(row[0])
        decision = decision_for_status(row[10] or "")
        jobs.append(
            SheetJob(
                real_job_id=real_job_id,
                source_job_ids=sources.get(real_job_id, ()),
                company=row[2] or "",
                title=row[3] or "",
                location=row[4] or "",
                url=row[5] or "",
                verdict=row[7],
                closed=bool(row[1] or row[6]),
                ignored=decision == "ignored",
                applied=real_job_id in applied or decision == "applied",
                queue=entries.get(real_job_id, ()),
                evaluated_at=row[9],
                funding=funding.get(normalize_company_name(row[2] or "")),
                **_evaluation(row[8]),
            )
        )
    return _with_pay(db, jobs, decisions, policy, today)


def _decisions(db: sqlite3.Connection) -> dict[int, SheetDecision]:
    return {
        int(row[0]): SheetDecision(SheetStatus(row[1]), row[2], row[3], row[4])
        for row in db.execute(
            "SELECT real_job_id,status,recruiter,note,updated_at "
            "FROM recruiter_sheet_rows"
        )
    }


def read_sheet(
    db_path: Path, policy: SheetPolicy, today: date | None = None
) -> list[SheetRow]:
    """Read the sheet without writing anything.

    Args:
        db_path: The Jobfeed SQLite database.
        policy: The configured rules.
        today: The date the funding window ends on, by default today in UTC.

    Returns:
        The rows, best fit first.

    Raises:
        sqlite3.Error: If the database or a table cannot be read.
        ValueError: If a stored status is not a known status.
    """
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=_BUSY_SECONDS)) as db:
        day = today or utc_today()
        decisions = _decisions(db)
        return build_sheet(_jobs(db, decisions, policy, day), decisions, policy, day)


def timestamp() -> str:
    """Format the current time as Jobfeed stores it, so times sort as text.

    Returns:
        A UTC timestamp.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_today() -> date:
    """Name today's date as Jobfeed's timestamps do.

    Returns:
        The date in UTC.
    """
    return datetime.now(UTC).date()


def apply_edits(
    db_path: Path,
    policy: SheetPolicy,
    edits: Iterable[SheetEdit],
    today: date | None = None,
) -> list[EditResult]:
    """Save a batch of edits, rejecting each bad one without touching the rest.

    Safe to repeat. An edit that changes nothing keeps the row's timestamp and creates
    no row, so sending back a whole sheet marks nothing changed and pins no job to it.

    Args:
        db_path: The Jobfeed SQLite database.
        policy: The configured rules.
        edits: The edits, applied in order. Each is keyed by real job ID.
        today: The date the funding window ends on, by default today in UTC.

    Returns:
        One result per edit, in the same order.

    Raises:
        sqlite3.Error: If the database or a table cannot be read or written.
        ValueError: If a stored status is not a known status.
    """
    now = timestamp()
    day = today or utc_today()
    with closing(sqlite3.connect(db_path, timeout=_BUSY_SECONDS)) as db:
        db.isolation_level = None
        db.execute("BEGIN IMMEDIATE")
        try:
            decisions = _decisions(db)
            jobs = _jobs(db, decisions, policy, day)
            on_sheet = {
                row.real_job_id for row in build_sheet(jobs, decisions, policy, day)
            }
            outcomes: list[tuple[SheetEdit, str, EditProblem | None]] = []
            for edit in edits:
                outcome, problem = _apply(db, edit, decisions, on_sheet, now)
                outcomes.append((edit, outcome, problem))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    rows = {row.real_job_id: row for row in build_sheet(jobs, decisions, policy, day)}
    return [
        EditResult(edit.real_job_id, outcome, problem, rows.get(edit.real_job_id))
        for edit, outcome, problem in outcomes
    ]


def save_funding(
    db_path: Path, company_key: str, company: str, funding: FundingRound | None
) -> None:
    """Store a company's funding check. A check never loses a stored round.

    Finding no round, or an earlier round than the stored one, only updates the check
    time. ISO dates compare correctly as text. Safe to repeat.

    Args:
        db_path: The Jobfeed SQLite database.
        company_key: The company's normalized name.
        company: The company's name as a posting writes it.
        funding: The verified round, or None when the check found none.

    Raises:
        sqlite3.Error: If the database or the table cannot be written.
    """
    now = timestamp()
    with closing(sqlite3.connect(db_path, timeout=_BUSY_SECONDS)) as db, db:
        db.execute(
            "UPDATE company_funding SET checked_at=? WHERE company_key=?",
            (now, company_key),
        )
        db.execute(
            "INSERT INTO company_funding(company_key,company,found,round,"
            "amount_usd,announced_on,source_url,checked_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company_key) DO UPDATE SET company=excluded.company,"
            "found=excluded.found,round=excluded.round,"
            "amount_usd=excluded.amount_usd,announced_on=excluded.announced_on,"
            "source_url=excluded.source_url,checked_at=excluded.checked_at "
            "WHERE company_funding.found=0 "
            "OR excluded.announced_on>=company_funding.announced_on",
            (
                company_key,
                company,
                int(funding is not None),
                funding.name if funding else None,
                funding.amount_usd if funding else None,
                funding.announced_on.isoformat() if funding else None,
                funding.source_url if funding else None,
                now,
            ),
        )


def forget_funding(db_path: Path, company_key: str) -> bool:
    """Delete a company's funding record, the way out for a wrong stored round.

    The company is then due a new check, and its jobs are pending and held until then.

    Args:
        db_path: The Jobfeed SQLite database.
        company_key: The company's normalized name.

    Returns:
        Whether a record existed.

    Raises:
        sqlite3.Error: If the database or the table cannot be written.
    """
    with closing(sqlite3.connect(db_path, timeout=_BUSY_SECONDS)) as db, db:
        deleted = db.execute(
            "DELETE FROM company_funding WHERE company_key=?", (company_key,)
        )
        return deleted.rowcount > 0


def funding_questions(
    db_path: Path, policy: SheetPolicy, today: date, limit: int
) -> list[FundingQuestion]:
    """List the companies whose funding check should run next.

    Args:
        db_path: The Jobfeed SQLite database.
        policy: The configured rules.
        today: The date the checks run on.
        limit: The most companies to list.

    Returns:
        One question per company, best fit first, each from its best-fit job.

    Raises:
        sqlite3.Error: If the database or a table cannot be read.
        ValueError: If a stored status or date is not valid.
    """
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=_BUSY_SECONDS)) as db:
        jobs = _jobs(db, _decisions(db), policy, today)
        keys = funding_lookups_needed(jobs, _funding(db), policy, today)[:limit]
        best: dict[str, SheetJob] = {}
        for job in sorted(jobs, key=lambda job: -(job.fit or 0)):
            best.setdefault(job.company_key, job)
        return [
            FundingQuestion(
                key,
                best[key].company,
                best[key].title,
                _posting(db, best[key].real_job_id),
            )
            for key in keys
        ]


def _posting(db: sqlite3.Connection, real_job_id: int) -> str:
    [text] = db.execute(
        "SELECT j.jd_text FROM real_jobs r JOIN jobs j "
        "ON j.id=r.representative_job_id WHERE r.id=?",
        (real_job_id,),
    ).fetchone()
    return text or ""


def _apply(
    db: sqlite3.Connection,
    edit: SheetEdit,
    decisions: dict[int, SheetDecision],
    on_sheet: set[int],
    now: str,
) -> tuple[str, EditProblem | None]:
    if edit.real_job_id not in on_sheet:
        return "rejected", _NOT_ON_SHEET
    current = decisions.get(edit.real_job_id, SheetDecision())
    problem = edit_problem(edit, current)
    if problem is not None:
        return "rejected", problem
    wanted = SheetDecision(
        SheetStatus(edit.status),
        current.recruiter if edit.recruiter is None else edit.recruiter.strip(),
        current.note if edit.note is None else edit.note.strip(),
    )
    if (wanted.status, wanted.recruiter, wanted.note) == (
        current.status,
        current.recruiter,
        current.note,
    ):
        return "unchanged", None
    db.execute(
        "INSERT INTO recruiter_sheet_rows"
        "(real_job_id,status,recruiter,note,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(real_job_id) DO UPDATE SET status=excluded.status,"
        "recruiter=excluded.recruiter,note=excluded.note,updated_at=excluded.updated_at",
        (edit.real_job_id, wanted.status.value, wanted.recruiter, wanted.note, now),
    )
    decisions[edit.real_job_id] = SheetDecision(
        wanted.status, wanted.recruiter, wanted.note, now
    )
    return "applied", None


def render_csv(rows: Iterable[SheetRow]) -> str:
    """Render the sheet as CSV with every text cell neutralized.

    Args:
        rows: The sheet.

    Returns:
        CSV text with a header row and ``real_job_id`` first as the stable key.
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                row.real_job_id,
                neutralize_cell(row.company),
                neutralize_cell(row.title),
                neutralize_cell(row.location),
                "" if row.fit is None else row.fit,
                neutralize_cell(row.verdict or ""),
                neutralize_cell(row.why),
                neutralize_cell(row.watch_out),
                row.url or "",
                row.status.value,
                neutralize_cell(row.recruiter),
                neutralize_cell(row.note),
                row.updated_at or "",
                "yes" if row.closed else "",
                "" if row.pay is None else row.pay,
                neutralize_cell(row.funding),
                row.funding_source or "",
                "yes" if row.funding_pending else "",
            ]
        )
    return output.getvalue()
