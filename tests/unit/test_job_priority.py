"""Executable ranking behavior from the confirmed job-search policy."""

from datetime import UTC, datetime, timedelta

import pytest

from jobfeed.domain.job_priority import (
    compensation_midpoint,
    freshness_value,
    new_grad_clarity,
    priority_for_job,
)
from jobfeed.domain.models import JobPosting, QualityBand

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def _job(**overrides: object) -> JobPosting:
    values: dict[str, object] = {
        "platform": "test",
        "canonical_id": "1",
        "url": "https://example.com/1",
        "title": "Software Engineer",
        "company": "Example",
        "location": "New York, NY",
        "discovered_at": NOW,
        "posted_at": NOW - timedelta(hours=4),
        "jd_text": "Build software services.",
        "jd_quality": QualityBand.FULL,
    }
    values.update(overrides)
    return JobPosting(**values)  # type: ignore[arg-type]


def test_primary_queue_always_precedes_internship() -> None:
    primary = priority_for_job(_job(), evidence_fit=10, now=NOW)
    intern = priority_for_job(
        _job(title="Software Engineering Intern"), evidence_fit=100, now=NOW
    )

    assert primary.queue_tier == 0
    assert intern.queue_tier == 1


def test_internship_threshold_changes_tier_without_filtering() -> None:
    assert (
        priority_for_job(
            _job(title="Software Engineering Intern"), evidence_fit=75, now=NOW
        ).queue_tier
        == 1
    )
    assert (
        priority_for_job(
            _job(title="Software Engineering Intern"), evidence_fit=74, now=NOW
        ).queue_tier
        == 2
    )


def test_new_grad_clarity_uses_confirmed_anchors() -> None:
    assert new_grad_clarity(_job(title="Software Engineer, New Graduate")) == 100
    assert new_grad_clarity(_job(title="Software Engineer I")) == 85
    assert new_grad_clarity(_job(jd_text="Requires 0-3 years of experience.")) == 55
    assert new_grad_clarity(_job(title="Founding Engineer")) == 40


def test_compensation_midpoint_normalizes_annual_and_hourly_ranges() -> None:
    assert compensation_midpoint("Base salary: $120,000-$180,000 per year") == 150000
    assert compensation_midpoint("Pay range is $50-$70 per hour") == 124800
    assert compensation_midpoint("Range: $140,000.00-$160,000.00.") == 150000


@pytest.mark.parametrize(
    ("text", "midpoint"),
    [
        (
            "The base salary for this role is anticipated to be between $150,000 and "
            "$200,000, which is based on experience.",
            175000,
        ),
        (
            "The base salary for this position is expected to be between $160,000.00 "
            "and $200,000",
            180000,
        ),
        ("Pay is between $150k and $200k per year.", 175000),
        ("Between $50 and $70 per hour.", 124800),
    ],
)
def test_compensation_midpoint_reads_a_between_range(text: str, midpoint: int) -> None:
    assert compensation_midpoint(text) == midpoint


@pytest.mark.parametrize(
    "text",
    [
        "We closed a $60m Series B and have $75m raised in total.",
        "We manage $71 billion in assets.",
        "We raised $150,000 and $200,000 from two angels.",
        "We raised between $60m and $75m.",
        "We raised between $100 million and $200 million.",
        "We raised between $100 and $200 million.",
        "We raised $100 to $200 million.",
        "A round of $100-$200.5M closed.",
        "Between $5 and $10 of every order goes to charity.",
    ],
)
def test_compensation_midpoint_takes_no_range_from_funding_text(text: str) -> None:
    assert compensation_midpoint(text) is None


def test_compensation_midpoint_ignores_malformed_range() -> None:
    assert compensation_midpoint("Salary: $120.000.00 - $202,000.00.") is None


def test_missing_ranking_evidence_is_neutral() -> None:
    result = priority_for_job(_job(posted_at=None), evidence_fit=None, now=NOW)

    assert result.compensation_score == 50
    assert result.company_strength_score == 50
    assert result.evidence_fit_score is None


def test_freshness_uses_whole_utc_calendar_day_buckets() -> None:
    almost_two_elapsed_days = _job(posted_at=datetime(2026, 9, 2, 0, 1, tzinfo=UTC))

    assert freshness_value(almost_two_elapsed_days, now=NOW) == 100
    assert (
        freshness_value(
            _job(posted_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC)), now=NOW
        )
        == 85
    )
