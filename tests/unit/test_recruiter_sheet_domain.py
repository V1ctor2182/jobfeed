"""Which jobs go on the recruiter sheet, and what the user may do with a row."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from jobfeed.config import RecruiterSheetSettings, Settings
from jobfeed.domain.recruiter_sheet import (
    TRANSITIONS,
    EditProblem,
    FundingRecord,
    FundingRound,
    QueueEntry,
    SheetDecision,
    SheetEdit,
    SheetJob,
    SheetPolicy,
    SheetStatus,
    build_sheet,
    disqualification,
    edit_problem,
    funding_lookups_needed,
    held_source_ids,
    neutralize_cell,
    parse_announced_on,
)

POLICY = SheetPolicy(excluded_companies=("Digital Waffle",))
TODAY = date(2026, 11, 7)
PAY = 150000
MIN_PAY = 130000


def funded(
    announced_on: date = date(2026, 3, 17), checked_on: date = date(2026, 11, 1)
) -> FundingRecord:
    return FundingRecord(
        FundingRound(
            "Series A", 17_000_000.0, announced_on, "https://example.com/round"
        ),
        checked_on,
    )


JOB = SheetJob(
    real_job_id=7,
    source_job_ids=(70, 71),
    company="Fixture Co",
    title="Software Engineer",
    location="New York, NY",
    url="https://fixture.invalid/7",
    verdict="apply",
    fit=85,
    major_gaps=("Airflow",),
    strengths=("Python", "Agents"),
    evaluated_at="2026-11-06T16:00:00.000000Z",
    pay=PAY,
    funding=funded(),
)


@pytest.mark.parametrize(
    "location",
    [
        "New York, NY",
        "New York, NY, USA",
        "New York, New York",
        "New York, United States",
        "New York City Metropolitan Area",
        "New York",
        "New York Office",
        "New York - Remote",
        "New York; Portland",
        "NYC",
        "HQ - NYC",
        "Boston or NYC",
        "Chicago, IL NYC",
        "4 locations Greenwich, CT Houston, TX Miami, FL NYC",
        "Hybrid - San Francisco, New York City, London",
        "San Francisco, CA | New York City, NY | Seattle, WA",
        "Charlotte, North Carolina, United States; New York, New York, United States",
        "Roivant Sciences, 1 Pennsylvania Plaza, New York, NY 10119, United States",
        "Credal HQ (Brooklyn, NY)",
        "Queens, NY",
        "Manhattan",
        "Manhattan, NY, USA",
    ],
)
def test_new_york_city_locations_match(location: str) -> None:
    assert disqualification(replace(JOB, location=location), POLICY, TODAY) is None


@pytest.mark.parametrize(
    "location",
    [
        "Buffalo, NY, USA",
        "Buffalo, New York, United States",
        "Monroe, NY",
        "Great Neck, NY, USA",
        "Rochester, NY",
        "Albany, New York Metropolitan Area",
        "Holtsville, New York",
        "White Plains, NY",
        "West New York, NJ",
        "New York Mills, NY",
        "Manhattan Beach, CA",
        "Brooklyn Park, MN",
        "NY",
        "Remote, USA",
        "",
    ],
)
def test_upstate_and_lookalike_locations_do_not_match(location: str) -> None:
    job = replace(JOB, location=location)
    assert disqualification(job, POLICY, TODAY) == "outside_locations"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"verdict": "consider"}, "verdict_not_apply"),
        ({"verdict": None}, "verdict_not_apply"),
        ({"fit": 77}, "fit_below_minimum"),
        ({"fit": None}, "fit_below_minimum"),
        ({"critical_gaps": 1}, "critical_gap"),
        ({"major_gaps": ("a", "b", "c")}, "too_many_major_gaps"),
        ({"title": "Software Engineering Intern, Frontend"}, "internship"),
        ({"title": "Software Engineer, Internship - Sovereign"}, "internship"),
        ({"title": "Engineer Co-op"}, "internship"),
        ({"company": "Digital Waffle LLC"}, "excluded_company"),
        ({"company": "TikTok"}, "excluded_company"),
        ({"closed": True}, "closed"),
        ({"ignored": True}, "ignored"),
        ({"applied": True}, "applied"),
        ({"queue": (QueueEntry("Applying", "claude:1"),)}, "queue_state"),
        ({"queue": (QueueEntry("Unavailable", None),)}, "queue_state"),
        ({"queue": (QueueEntry("Queued", "claude:1"),)}, "queue_state"),
        ({"pay": None}, "pay_too_low"),
        ({"pay": 129999}, "pay_too_low"),
        ({"funding": FundingRecord(None, TODAY)}, "no_recent_funding"),
        (
            {"queue": (QueueEntry("Queued", None), QueueEntry("Applying", "claude:1"))},
            "queue_state",
        ),
    ],
)
def test_each_rule_excludes(change: dict[str, object], rule: str) -> None:
    assert disqualification(replace(JOB, **change), POLICY, TODAY) == rule  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"fit": 78},
        {"major_gaps": ("a", "b")},
        {"queue": (QueueEntry("Queued", None),)},
        {"title": "Internal Tools Engineer"},
        {"pay": MIN_PAY},
    ],
)
def test_boundaries_still_qualify(change: dict[str, object]) -> None:
    assert disqualification(replace(JOB, **change), POLICY, TODAY) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("announced_on", "rule"),
    [
        (date(2025, 11, 7), None),
        (date(2025, 11, 6), "no_recent_funding"),
        (TODAY, None),
        (date(2026, 11, 8), "no_recent_funding"),
        (parse_announced_on("2025-12"), None),
        (parse_announced_on("2025-11"), "no_recent_funding"),
        (parse_announced_on("2026-11"), None),
    ],
)
def test_the_funding_window_is_calendar_months_ending_today(
    announced_on: date, rule: str | None
) -> None:
    job = replace(JOB, funding=funded(announced_on))
    assert disqualification(job, POLICY, TODAY) == rule


@pytest.mark.parametrize(
    ("today", "months", "earliest"),
    [
        (date(2027, 3, 31), 1, date(2027, 2, 28)),
        (date(2028, 3, 31), 1, date(2028, 2, 29)),
        (date(2027, 1, 15), 1, date(2026, 12, 15)),
        (date(2026, 12, 31), 12, date(2025, 12, 31)),
    ],
)
def test_the_funding_window_counts_back_across_short_months_and_years(
    today: date, months: int, earliest: date
) -> None:
    policy = SheetPolicy(funding_within_months=months)
    job = replace(JOB, funding=funded(earliest))
    assert disqualification(job, policy, today) is None
    day_before = date.fromordinal(earliest.toordinal() - 1)
    job = replace(JOB, funding=funded(day_before))
    assert disqualification(job, policy, today) == "no_recent_funding"


def test_a_job_with_no_record_is_on_the_sheet_pending_and_held() -> None:
    job = replace(JOB, funding=None)
    [row] = build_sheet([job], {}, POLICY, TODAY)
    assert row.qualifies
    assert row.funding_pending
    assert (row.funding, row.funding_source) == ("", None)
    assert held_source_ids([row]) == {70, 71}


def test_a_failing_rule_outranks_a_pending_check() -> None:
    assert build_sheet([replace(JOB, funding=None, pay=None)], {}, POLICY, TODAY) == []
    decision = SheetDecision(SheetStatus.ASKED)
    [row] = build_sheet(
        [replace(JOB, funding=None, pay=None)], {7: decision}, POLICY, TODAY
    )
    assert not row.funding_pending


def test_a_stale_record_stays_in_force() -> None:
    stale = funded(date(2026, 3, 17), checked_on=date(2026, 1, 1))
    assert disqualification(replace(JOB, funding=stale), POLICY, TODAY) is None
    old = funded(date(2024, 3, 17), checked_on=date(2026, 1, 1))
    assert disqualification(replace(JOB, funding=old), POLICY, TODAY) == (
        "no_recent_funding"
    )


def test_a_job_leaves_the_sheet_with_no_decision_when_its_company_has_no_round() -> (
    None
):
    job = replace(JOB, funding=FundingRecord(None, TODAY))
    assert build_sheet([job], {}, POLICY, TODAY) == []


def test_a_row_shows_pay_and_funding() -> None:
    [row] = build_sheet([JOB], {}, POLICY, TODAY)
    assert row.pay == PAY
    assert row.funding == "Series A, $17M, 2026-03-17"
    assert row.funding_source == "https://example.com/round"
    assert not row.funding_pending


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (None, "Series A, 2026-03-17"),
        (17_500_000.0, "Series A, $17.5M, 2026-03-17"),
        (1_200_000_000.0, "Series A, $1.2B, 2026-03-17"),
        (850_000.0, "Series A, $850K, 2026-03-17"),
        (500.0, "Series A, $500, 2026-03-17"),
    ],
)
def test_funding_text_shows_the_amount_when_there_is_one(
    amount: float | None, text: str
) -> None:
    record = FundingRecord(
        FundingRound("Series A", amount, date(2026, 3, 17), "https://example.com/a"),
        TODAY,
    )
    [row] = build_sheet([replace(JOB, funding=record)], {}, POLICY, TODAY)
    assert row.funding == text


def test_a_kept_row_says_none_was_found() -> None:
    job = replace(JOB, funding=FundingRecord(None, TODAY))
    [row] = build_sheet([job], {7: SheetDecision(SheetStatus.ASKED)}, POLICY, TODAY)
    assert (row.funding, row.funding_source, row.funding_pending) == (
        "None found",
        None,
        False,
    )


@pytest.mark.parametrize(
    ("text", "day"),
    [("2026-03-17", date(2026, 3, 17)), ("2026-03", date(2026, 3, 1))],
)
def test_a_funding_date_reads_as_a_day_or_a_month(text: str, day: date) -> None:
    assert parse_announced_on(text) == day


@pytest.mark.parametrize(
    "text", ["March 2026", "2026", "2026-13", "2026-02-30", "20260317", "2026-3-17"]
)
def test_any_other_funding_date_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        parse_announced_on(text)


def company_job(real_id: int, company: str, fit: int, **changes: object) -> SheetJob:
    return replace(
        JOB,
        real_job_id=real_id,
        company=company,
        fit=fit,
        funding=None,
        **changes,  # type: ignore[arg-type]
    )


def test_lookups_cover_companies_passing_every_other_rule_best_fit_first() -> None:
    jobs = [
        company_job(1, "Low Co", 80),
        company_job(2, "High, Inc.", 95),
        company_job(3, "High", 90),
        company_job(4, "Closed Co", 99, closed=True),
        company_job(5, "Poor Co", 99, pay=90000),
        company_job(6, "No Pay Co", 99, pay=None),
        company_job(7, "!!!", 99),
    ]
    assert funding_lookups_needed(jobs, {}, POLICY, TODAY) == ["high", "low"]


def test_lookups_skip_a_recent_record_and_retry_one_over_thirty_days_old() -> None:
    jobs = [company_job(1, "Fresh", 90), company_job(2, "Stale", 85)]
    records = {
        "fresh": funded(checked_on=date(2026, 10, 8)),
        "stale": FundingRecord(None, date(2026, 10, 7)),
    }
    assert funding_lookups_needed(jobs, records, POLICY, TODAY) == ["stale"]


def test_rows_are_sorted_by_fit_with_short_why_and_watch_out() -> None:
    long = "x" * 500
    rows = build_sheet(
        [
            replace(JOB, real_job_id=1, fit=80),
            replace(
                JOB,
                real_job_id=2,
                fit=90,
                strengths=("a", "b", "c", "d"),
                major_gaps=(long,),
            ),
        ],
        {},
        POLICY,
        TODAY,
    )
    assert [row.real_job_id for row in rows] == [2, 1]
    assert rows[0].why == "a; b; c"
    assert rows[0].watch_out.endswith("…")
    assert len(rows[0].watch_out) < len(long)
    assert rows[0].status is SheetStatus.TO_ASK
    assert rows[0].status_updated_at is None
    assert rows[0].updated_at == JOB.evaluated_at


def test_a_job_with_no_decision_leaves_when_it_stops_qualifying() -> None:
    assert build_sheet([replace(JOB, fit=10)], {}, POLICY, TODAY) == []


@pytest.mark.parametrize(
    "change", [{"fit": 10}, {"closed": True}, {"applied": True}, {"verdict": "skip"}]
)
def test_a_stored_decision_keeps_the_job_on_the_sheet(
    change: dict[str, object],
) -> None:
    decision = SheetDecision(
        SheetStatus.WITH_RECRUITER, "Sam", "intro sent", "2026-11-07T00:00:00.000000Z"
    )
    rows = build_sheet([replace(JOB, **change)], {7: decision}, POLICY, TODAY)  # type: ignore[arg-type]
    assert [(row.status, row.recruiter, row.qualifies) for row in rows] == [
        (SheetStatus.WITH_RECRUITER, "Sam", False)
    ]
    assert rows[0].updated_at == decision.updated_at
    assert rows[0].status_updated_at == decision.updated_at
    assert rows[0].closed is change.get("closed", False)


def test_only_apply_myself_releases_the_hold() -> None:
    other = replace(JOB, real_job_id=8, source_job_ids=(80,))
    rows = build_sheet(
        [JOB, other], {8: SheetDecision(SheetStatus.APPLY_MYSELF)}, POLICY, TODAY
    )
    assert held_source_ids(rows) == {70, 71}
    for status in set(SheetStatus) - {SheetStatus.APPLY_MYSELF}:
        rows = build_sheet([other], {8: SheetDecision(status)}, POLICY, TODAY)
        assert held_source_ids(rows) == {80}


def test_every_status_has_a_transition_entry_that_never_lists_itself() -> None:
    assert set(TRANSITIONS) == set(SheetStatus)
    assert all(status not in TRANSITIONS[status] for status in SheetStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SheetStatus.TO_ASK, SheetStatus.ASKED),
        (SheetStatus.TO_ASK, SheetStatus.WITH_RECRUITER),
        (SheetStatus.ASKED, SheetStatus.WITH_RECRUITER),
        (SheetStatus.WITH_RECRUITER, SheetStatus.PASSED),
        (SheetStatus.WITH_RECRUITER, SheetStatus.APPLY_MYSELF),
        (SheetStatus.PASSED, SheetStatus.APPLY_MYSELF),
        (SheetStatus.APPLY_MYSELF, SheetStatus.ASKED),
        (SheetStatus.ASKED, SheetStatus.ASKED),
    ],
)
def test_allowed_transitions(current: SheetStatus, target: SheetStatus) -> None:
    edit = SheetEdit(7, target.value)
    assert edit_problem(edit, SheetDecision(current)) is None


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SheetStatus.WITH_RECRUITER, SheetStatus.TO_ASK),
        (SheetStatus.APPLY_MYSELF, SheetStatus.WITH_RECRUITER),
        (SheetStatus.PASSED, SheetStatus.WITH_RECRUITER),
    ],
)
def test_refused_transitions(current: SheetStatus, target: SheetStatus) -> None:
    problem = edit_problem(SheetEdit(7, target.value), SheetDecision(current))
    assert isinstance(problem, EditProblem)
    assert problem.code == "invalid_transition"


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (SheetEdit(7, "With Recruiter"), "unknown_status"),
        (SheetEdit(7, ""), "unknown_status"),
        (SheetEdit(7, "asked", recruiter="x" * 201), "too_long"),
        (SheetEdit(7, "asked", note="x" * 4001), "too_long"),
    ],
)
def test_malformed_edits_are_refused(edit: SheetEdit, code: str) -> None:
    problem = edit_problem(edit, SheetDecision())
    assert problem is not None
    assert problem.code == code


@pytest.mark.parametrize(
    "value",
    [
        "=cmd()",
        "+1",
        "-1",
        "@SUM(A1)",
        "\tx",
        "\rx",
        " =cmd()",
        "\n=cmd()",
    ],
)
def test_formula_cells_are_neutralized(value: str) -> None:
    assert neutralize_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["Fixture Co", "", "a=b", "C++ Engineer", "e-mail"])
def test_ordinary_cells_are_unchanged(value: str) -> None:
    assert neutralize_cell(value) == value


def test_posting_urls_other_than_http_are_dropped() -> None:
    rows = build_sheet([replace(JOB, url="javascript:alert(1)")], {}, POLICY, TODAY)
    assert rows[0].url is None


def test_settings_default_without_a_section_and_forbid_unknown_keys() -> None:
    settings = Settings()
    policy = settings.recruiter_sheet.to_domain()
    assert (policy.min_fit, policy.max_major_gaps, policy.excluded_companies) == (
        78,
        2,
        (),
    )
    assert disqualification(JOB, policy, TODAY) is None
    with pytest.raises(ValidationError):
        RecruiterSheetSettings.model_validate({"cities": []})


def test_settings_set_the_pay_floor_and_the_funding_window() -> None:
    policy = Settings().recruiter_sheet.to_domain()
    assert (policy.min_pay, policy.funding_within_months) == (130000, 12)
    policy = RecruiterSheetSettings(min_pay=200000, funding_within_months=6).to_domain()
    assert (policy.min_pay, policy.funding_within_months) == (200000, 6)
    with pytest.raises(ValidationError):
        RecruiterSheetSettings(funding_within_months=0)


def test_settings_refuse_a_location_that_is_not_a_pattern() -> None:
    with pytest.raises(ValidationError):
        RecruiterSheetSettings(locations=["(new york"])


def test_location_config_replaces_the_default() -> None:
    policy = RecruiterSheetSettings(locations=["boston"]).to_domain()
    assert disqualification(replace(JOB, location="Boston, MA"), policy, TODAY) is None
    assert disqualification(JOB, policy, TODAY) == "outside_locations"
