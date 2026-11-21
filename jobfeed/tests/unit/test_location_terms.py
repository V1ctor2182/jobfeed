"""Allow-list locations written the way profiles say them still match job sites."""

from __future__ import annotations

from jobfeed.config import HardFiltersSettings
from jobfeed.domain.filtering import apply_hard_filters
from jobfeed.domain.location_terms import location_terms
from tests.support.factories import make_job


def test_notes_are_dropped_and_a_bracketed_place_also_matches_after_a_comma() -> None:
    assert location_terms(
        ["Seattle, WA (preferred)", "Remote (US)", " ", "Seattle, WA"]
    ) == ["Seattle, WA", "Remote (US)", "Remote, US"]


def test_a_profile_written_with_notes_keeps_the_jobs_sites_list() -> None:
    """The Nov 21 box test lost 14 "Remote, US" jobs to "Remote (US)"."""
    filters = HardFiltersSettings(
        location_allowlist=["Seattle, WA (preferred)", "Remote (US)"]
    ).to_domain()

    assert apply_hard_filters(make_job(location="Remote, US"), filters) is None
    assert apply_hard_filters(make_job(location="Seattle, WA, US"), filters) is None
    assert (
        apply_hard_filters(make_job(location="Austin, TX"), filters)
        == "location not in allowlist"
    )
