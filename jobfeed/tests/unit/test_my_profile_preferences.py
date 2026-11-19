"""Preferences edits apply supported filters while preserving user-owned drafts."""

from pathlib import Path

import pytest

from jobfeed.config import Settings
from jobfeed.config_editor import ConfigurationEditor
from jobfeed.my_profile_preferences import ProfilePreferencesService
from jobfeed.onboarding_resume_files import _write_private_bytes
from jobfeed.onboarding_resume_types import JobProfile, ResumeDraftState
from jobfeed.onboarding_searches import (
    OnboardingSearchStore,
    SearchSuggestion,
    searches_need_review,
)

ACTIVE_POSTING_AGE_HOURS = 48


def _profile() -> JobProfile:
    return JobProfile(
        desired_titles=["Designer"],
        seniority_levels=["Senior"],
        target_countries=["United States"],
        target_locations=["Boston"],
        work_modes=["remote"],
        industries=["Education"],
        company_sizes=[],
        work_authorization="User-entered authorization",
        hiring_timeline="User-entered timeline",
        excluded_titles=["Sales"],
        excluded_companies=["Example Company"],
        excluded_locations=["Chicago"],
        excluded_keywords=["commission only"],
        maximum_posting_age_hours=72,
        resume_evidence=[],
    )


def _service(root: Path) -> ProfilePreferencesService:
    settings = Settings()
    settings.sources.indeed.search_urls = ["https://www.indeed.com/jobs?q=custom"]
    return ProfilePreferencesService(
        root, ConfigurationEditor(root / "config.toml", settings, lambda _: None)
    )


def _upload(root: Path) -> None:
    _write_private_bytes(root / "data/resumes/existing.txt", b"Existing resume text")
    draft = ResumeDraftState(
        stored_name="existing.txt",
        original_name="existing.txt",
        extracted_text="Existing resume text",
    )
    _write_private_bytes(
        root / "data/onboarding-resume.json", draft.model_dump_json().encode()
    )


def _custom_search() -> SearchSuggestion:
    return SearchSuggestion(
        id="user-search",
        source="indeed",
        query="Custom role",
        location="Boston",
        url="https://www.indeed.com/jobs?q=custom",
        enabled=True,
    )


def test_new_preferences_are_editable_without_ai_or_resume(tmp_path: Path) -> None:
    service = _service(tmp_path)
    state = service.state()
    assert state.profile.work_authorization == ""
    assert state.scoring_resume_active is False
    assert state.searches_need_review is True
    assert not (tmp_path / "data/onboarding-resume.json").exists()

    saved = service.save(_profile())

    assert saved.profile == _profile()
    assert service.drafts.load().is_confirmed is True
    assert saved.scoring_resume_active is False


def test_preferences_apply_filters_and_resume_preserving_custom_sources(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _upload(tmp_path)
    original_sources = service.editor.editable.sources

    saved = service.save(_profile())
    active = service.editor.editable

    assert active.hard_filters.model_dump() == {
        "title_blocklist": ["Sales"],
        "company_blocklist": ["Example Company"],
        "location_allowlist": ["Boston"],
        "location_blocklist": ["Chicago"],
        "posted_within_hours": 72,
        "posted_within_days": None,
        "big_company_list": [],
        "big_company_days": 90,
    }
    assert active.sources == original_sources
    assert active.llm.master_resume_path == "data/resumes/existing.txt"
    assert saved.scoring_resume_active is True
    assert service.state().profile == _profile()
    assert service.save(_profile()) == saved


def test_preference_changes_require_search_review_without_erasing_edits(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.save(_profile())
    store = OnboardingSearchStore(tmp_path / "data/onboarding-searches.json")
    store.save([_custom_search()], _profile())
    assert service.state().searches_need_review is False
    updated = _profile().model_copy(update={"desired_titles": ["Product Designer"]})

    assert service.save(updated).searches_need_review is True
    assert store.for_profile(updated).searches == [_custom_search()]
    assert service.state().searches_need_review is True

    store.save([_custom_search()], updated)
    assert service.state().searches_need_review is False


def test_old_search_draft_requires_review_and_keeps_custom_rows(tmp_path: Path) -> None:
    store = OnboardingSearchStore(tmp_path / "searches.json")
    _write_private_bytes(
        tmp_path / "searches.json",
        ('{"searches": [' + _custom_search().model_dump_json() + "]}").encode(),
    )
    assert store.for_profile(_profile()).searches == [_custom_search()]
    assert searches_need_review(_profile(), store.load()) is True


def test_unrelated_preference_edit_does_not_invalidate_search_review(
    tmp_path: Path,
) -> None:
    store = OnboardingSearchStore(tmp_path / "searches.json")
    saved = store.save([_custom_search()], _profile())
    updated = _profile().model_copy(update={"hiring_timeline": "Next month"})
    assert searches_need_review(updated, saved) is False


def test_draft_failure_restores_active_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    previous = service.editor.editable

    def fail(_profile: JobProfile) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service.drafts, "save_preferences", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        service.save(_profile())
    assert service.editor.editable == previous
    assert service.drafts.load().profile is None


def test_configuration_failure_does_not_confirm_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)

    def fail(_editable: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service.editor, "save", fail)
    with pytest.raises(RuntimeError, match="could not be saved completely"):
        service.save(_profile())
    assert service.drafts.load().profile is None


def test_unsafe_or_missing_upload_does_not_replace_scoring_resume(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    draft = ResumeDraftState(stored_name="../outside.txt", extracted_text="Existing")
    _write_private_bytes(
        tmp_path / "data/onboarding-resume.json", draft.model_dump_json().encode()
    )
    previous = service.editor.editable.llm.master_resume_path
    assert service.save(_profile()).scoring_resume_active is False
    assert service.editor.editable.llm.master_resume_path == previous


@pytest.mark.parametrize("locations", [[], ["United States"], ["California"]])
def test_active_filters_override_old_confirmed_profile_without_losing_search_fields(
    tmp_path: Path, locations: list[str]
) -> None:
    service = _service(tmp_path)
    service.save(_profile())
    active = service.editor.editable
    active.hard_filters.location_allowlist = locations
    active.hard_filters.title_blocklist = ["Recruiter"]
    active.hard_filters.company_blocklist = ["Haystack", "Staffing Company"]
    active.hard_filters.location_blocklist = ["Excluded Location"]
    active.hard_filters.posted_within_hours = ACTIVE_POSTING_AGE_HOURS
    service.editor.save(active)

    profile = service.state().profile
    assert profile.target_locations == locations
    assert profile.target_countries == []
    assert profile.excluded_titles == ["Recruiter"]
    assert profile.excluded_companies == ["Haystack", "Staffing Company"]
    assert profile.excluded_locations == ["Excluded Location"]
    assert profile.maximum_posting_age_hours == ACTIVE_POSTING_AGE_HOURS
    assert profile.desired_titles == ["Designer"]
    assert profile.work_modes == ["remote"]
    assert profile.excluded_keywords == ["commission only"]

    profile.hiring_timeline = "Next month"
    service.save(profile)
    assert service.editor.editable.hard_filters == active.hard_filters


@pytest.mark.parametrize("locations", [[], ["Boston"]])
def test_matching_country_and_location_preferences_keep_their_representation(
    tmp_path: Path, locations: list[str]
) -> None:
    service = _service(tmp_path)
    profile = _profile().model_copy(update={"target_locations": locations})
    assert service.save(profile).profile == profile
    assert service.state().profile == profile


def test_unconfirmed_suggestions_remain_reviewable_on_configured_install(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.editor.save(service.editor.editable)
    _upload(tmp_path)
    service.drafts.save_profile(_profile(), is_confirmed=False)
    assert service.state().profile == _profile()
    assert service.editor.editable.hard_filters.company_blocklist == []


def test_legacy_confirmed_profile_is_visible_before_first_configuration_save(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _upload(tmp_path)
    service.drafts.save_profile(_profile(), is_confirmed=True)
    assert service.state().profile == _profile()
    assert not service.editor.is_configured
