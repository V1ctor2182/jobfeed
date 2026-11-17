"""User-owned job preferences and their supported active discovery filters."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jobfeed.config import HardFiltersSettings
from jobfeed.config_editor import ConfigurationEditor, EditableConfiguration
from jobfeed.onboarding_resume import ResumeDraftStore
from jobfeed.onboarding_resume_files import private_file_lock
from jobfeed.onboarding_resume_types import JobProfile, ResumeDraftState
from jobfeed.onboarding_searches import OnboardingSearchStore, searches_need_review

PROFILE_FILTER_FIELDS = frozenset(
    {
        "title_blocklist",
        "company_blocklist",
        "location_allowlist",
        "location_blocklist",
        "posted_within_hours",
        "posted_within_days",
    }
)


def profile_hard_filters(
    profile: JobProfile, current: HardFiltersSettings
) -> HardFiltersSettings:
    """Project confirmed preferences while retaining other filter settings.

    Args:
        profile: Confirmed job preferences.
        current: Hard filter settings currently in effect.

    Returns:
        A copy of `current` with the block lists, location allow list and
        posting age taken from the profile.
    """
    return current.model_copy(
        update={
            "title_blocklist": profile.excluded_titles,
            "company_blocklist": profile.excluded_companies,
            "location_allowlist": profile.target_locations or profile.target_countries,
            "location_blocklist": profile.excluded_locations,
            "posted_within_hours": profile.maximum_posting_age_hours,
            "posted_within_days": None,
        }
    )


class ProfilePreferencesState(BaseModel):
    """Saved preferences with separate search-review and scoring readiness."""

    model_config = ConfigDict(extra="forbid")

    profile: JobProfile
    searches_need_review: bool
    scoring_resume_active: bool


class ProfilePreferencesService:
    """Apply supported filters without touching search rows or application authority."""

    def __init__(self, root: Path, editor: ConfigurationEditor) -> None:
        self.root = root.resolve()
        self.editor = editor
        self.drafts = ResumeDraftStore(self.root / "data/onboarding-resume.json")
        self.searches = OnboardingSearchStore(
            self.root / "data/onboarding-searches.json"
        )

    def state(self) -> ProfilePreferencesState:
        """Read the preference draft, using the active settings once confirmed.

        Returns:
            The preference draft and its status.
        """
        draft = self.drafts.load()
        profile = draft.profile or self._initial_profile()
        if draft.is_confirmed and self.editor.is_configured:
            active = self._initial_profile()
            updates = {
                name: getattr(active, name)
                for name in (
                    "excluded_titles",
                    "excluded_companies",
                    "excluded_locations",
                    "maximum_posting_age_hours",
                    "maximum_posting_age_days",
                )
            }
            if (profile.target_locations or profile.target_countries) != (
                active.target_locations
            ):
                updates.update(
                    target_countries=[], target_locations=active.target_locations
                )
            profile = profile.model_copy(update=updates)
        uploaded = self._uploaded_resume(draft)
        selected = Path(self.editor.editable.llm.master_resume_path).expanduser()
        if not selected.is_absolute():
            selected = self.root / selected
        return ProfilePreferencesState(
            profile=profile,
            searches_need_review=searches_need_review(profile, self.searches.load()),
            scoring_resume_active=bool(
                uploaded is not None and selected.resolve() == uploaded
            ),
        )

    def save(self, profile: JobProfile) -> ProfilePreferencesState:
        """Save job preferences under the preferences file lock.

        Args:
            profile: Job preferences to save.

        Returns:
            The saved preference state.
        """
        with private_file_lock(self.root / "data/my-profile-preferences.lock"):
            return self._save(profile)

    def _save(self, profile: JobProfile) -> ProfilePreferencesState:
        self.searches.load()
        previous = self.editor.editable
        payload = previous.model_dump(mode="json")
        payload["hard_filters"] = profile_hard_filters(
            profile, previous.hard_filters
        ).model_dump(mode="json")
        uploaded = self._uploaded_resume(self.drafts.load())
        if uploaded is not None:
            payload["llm"]["master_resume_path"] = str(uploaded.relative_to(self.root))
        editable = EditableConfiguration.model_validate(payload)
        try:
            self.editor.save(editable)
            self.drafts.save_preferences(profile)
        except Exception:
            try:
                self.editor.save(previous)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Preferences could not be saved completely. "
                    "Review active settings before retrying."
                ) from rollback_error
            raise
        return self.state()

    def _uploaded_resume(self, draft: ResumeDraftState) -> Path | None:
        name = draft.stored_name
        if not name or Path(name).name != name:
            return None
        directory = self.root / "data/resumes"
        candidate = (directory / name).resolve()
        if candidate.parent != directory.resolve():
            return None
        if not candidate.is_file() or candidate.stat().st_size == 0:
            return None
        return candidate

    def _initial_profile(self) -> JobProfile:
        active = self.editor.editable
        filters = active.hard_filters
        age = filters.posted_within_hours
        if age is None and filters.posted_within_days is not None:
            age = filters.posted_within_days * 24
        return JobProfile(
            desired_titles=list(active.sources.ats.title_keywords),
            seniority_levels=[],
            target_countries=[],
            target_locations=list(filters.location_allowlist),
            work_modes=[],
            industries=[],
            company_sizes=[],
            work_authorization="",
            hiring_timeline="",
            excluded_titles=list(filters.title_blocklist),
            excluded_companies=list(filters.company_blocklist),
            excluded_locations=list(filters.location_blocklist),
            excluded_keywords=[],
            maximum_posting_age_hours=age or 36,
            resume_evidence=[],
        )
