"""Local profile preferences editing without imports or application dispatch."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from jobfeed.config_editor import ConfigurationEditor
from jobfeed.my_profile_preferences import (
    ProfilePreferencesService,
    ProfilePreferencesState,
)
from jobfeed.onboarding_resume_types import JobProfile
from jobfeed.web.deps import get_configuration_editor
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.my_profile import _mutation

router = APIRouter()
_Editor = Annotated[ConfigurationEditor, Depends(get_configuration_editor)]


@router.get("/my-profile/preferences", response_model=ProfilePreferencesState)
def get_profile_preferences(
    request: Request, response: Response, editor: _Editor
) -> ProfilePreferencesState:
    """Return the saved job preferences.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.
        editor: Configuration editor for the active settings.

    Returns:
        The preference state.

    Raises:
        ApiError: If the preferences cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    service = ProfilePreferencesService(request.app.state.onboarding_root, editor)
    try:
        return service.state()
    except (ValueError, OSError) as exc:
        raise ApiError(
            500,
            "profile_preferences_unavailable",
            "Saved preferences could not be read.",
        ) from exc


@router.put("/my-profile/preferences", response_model=ProfilePreferencesState)
def put_profile_preferences(
    body: JobProfile, request: Request, response: Response, editor: _Editor
) -> ProfilePreferencesState:
    """Save job preferences and apply them to the active settings.

    Args:
        body: Job preferences to save.
        request: Incoming request, checked as a dashboard mutation.
        response: Response whose cache headers are set.
        editor: Configuration editor for the active settings.

    Returns:
        The saved preference state.

    Raises:
        ApiError: If the preferences cannot be saved completely.
    """
    _mutation(request)
    response.headers["Cache-Control"] = "no-store"
    service = ProfilePreferencesService(request.app.state.onboarding_root, editor)
    try:
        return service.save(body)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ApiError(
            500,
            "profile_preferences_not_saved",
            "Preferences could not be saved completely. Review the current values "
            "and active settings before retrying.",
        ) from exc
