"""Expose local setup evidence without starting imports or applications."""

from __future__ import annotations

from fastapi import APIRouter, Request

from jobfeed.onboarding_checklist import (
    ChecklistInputs,
    OnboardingChecklist,
    build_checklist,
)
from jobfeed.onboarding_resume_types import ResumeDraftState
from jobfeed.onboarding_searches import OnboardingSearchStore, SearchDraftState
from jobfeed.onboarding_types import ProviderOnboardingState
from jobfeed.web.dashboard_origin import dashboard_loopback_origin

router = APIRouter()


@router.get("/onboarding/checklist", response_model=OnboardingChecklist)
def get_onboarding_checklist(request: Request) -> OnboardingChecklist:
    """Return the setup checklist, treating unreadable drafts as empty.

    Args:
        request: Incoming request carrying the app state.

    Returns:
        The setup checklist.
    """
    state = request.app.state
    root = state.onboarding_root
    try:
        provider = state.onboarding_provider_service.state()
    except (OSError, ValueError, TypeError):
        provider = ProviderOnboardingState()
    try:
        resume = state.onboarding_resume_service.state()
    except (OSError, ValueError, TypeError):
        resume = ResumeDraftState()
    try:
        searches = OnboardingSearchStore(root / "data/onboarding-searches.json").load()
    except (OSError, ValueError, TypeError):
        searches = SearchDraftState()
    try:
        readiness = state.apply_readiness.get()
    except (OSError, RuntimeError, ValueError):
        readiness = None
    return build_checklist(
        ChecklistInputs(
            root=root,
            database=state.context["settings"].db.path,
            apply_root=state.apply_health_reader.root,
            active=state.configuration_editor.editable,
            provider=provider,
            resume=resume,
            searches=searches,
            readiness=readiness,
            dashboard_origin=dashboard_loopback_origin(request),
        )
    )
