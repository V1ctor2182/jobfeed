"""Unit tests for deterministic hard filters.

NOTE: title_blocklist was removed from apply_hard_filters in Phase 5 — that
logic moved to the ML gate. Title-based tests are now in test_hard_filter.py
(which asserts that title_blocklist does NOT block).
"""

from __future__ import annotations

import pytest

from jobfeed.domain.filtering import HardFilters, _location_reason, apply_hard_filters
from tests.support.factories import make_job


def test_apply_hard_filters_blocks_company_word() -> None:
    """Company blocklist matches should return a filter reason."""
    filters = HardFilters(title_blocklist=[], company_blocklist=["agency"])

    reason = apply_hard_filters(make_job(company="Hiring Agency"), filters)

    assert reason == 'company contains "agency"'


def test_apply_hard_filters_passes_clean_job() -> None:
    """Jobs without blocklist matches should pass."""
    filters = HardFilters(title_blocklist=["sales"], company_blocklist=["agency"])

    assert apply_hard_filters(make_job(), filters) is None


def test_united_states_allowlist_accepts_state_codes_and_rejects_foreign_jobs() -> None:
    """The GUI's United States choice recognizes normal ATS location formats."""
    filters = HardFilters(location_allowlist=["United States"])

    assert apply_hard_filters(make_job(location="San Francisco, CA"), filters) is None
    assert (
        apply_hard_filters(make_job(location="United States, Remote"), filters) is None
    )
    assert (
        apply_hard_filters(make_job(location="Ljubljana, Slovenia"), filters)
        == "location not in allowlist"
    )


@pytest.mark.parametrize(
    "location",
    [
        "California", "New York", "New Mexico", "District of Columbia",
        "San Francisco CA", "New York, NY", "Portland, OR", "Indianapolis, IN",
        "Remote - US", "US", "USA", "U.S.", "U.S.A.", "united states",
        "United States of America", "Remote, california",
    ],
)
def test_us_filter_accepts_explicit_us_locations(location: str) -> None:
    assert apply_hard_filters(
        make_job(location=location), HardFilters(location_allowlist=["United States"])
    ) is None


@pytest.mark.parametrize(
    "location",
    [
        "", "Unknown", "Remote", "Multiple locations", "N/A",
        "Remote in India", "Berlin, DE, Germany", "Toronto, ON, Canada",
        "London, UK", "United States / Canada", "Mexico", "India, IN",
        "Remote in Europe", "London or Paris", "near me", "Jerusalem", "Busan",
    ],
)
def test_us_filter_rejects_foreign_or_unconfirmed_locations(location: str) -> None:
    assert apply_hard_filters(
        make_job(location=location), HardFilters(location_allowlist=["United States"])
    ) == "location not in allowlist"


def test_us_filter_requires_evidence_when_location_is_missing() -> None:
    assert _location_reason(None, ["United States"], []) == "location not in allowlist"
    assert _location_reason(None, ["Remote"], []) is None


def test_non_us_explicit_allowlist_choice_is_preserved() -> None:
    assert apply_hard_filters(
        make_job(location="Toronto, Canada"),
        HardFilters(location_allowlist=["United States", "Canada"]),
    ) is None
