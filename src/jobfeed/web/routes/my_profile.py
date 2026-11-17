"""Same-origin local editing of personal facts and application PDF selections."""

from __future__ import annotations

from ipaddress import ip_address
from typing import cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse

from jobfeed.my_profile import MyProfileService
from jobfeed.my_profile_document import ProfileConflict
from jobfeed.my_profile_models import (
    AddCurrentResume,
    MyProfileState,
    PersonalDetailsUpdate,
    ResumeActiveUpdate,
    ResumeAvailabilityUpdate,
)
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.application_tabs import _allowed_origin
from jobfeed.resume_library_types import ResumeUploadMetadata

router = APIRouter()


def _service(request: Request) -> MyProfileService:
    return cast(MyProfileService, request.app.state.my_profile_service)


def _local_dashboard_proxy(request: Request, origin: str) -> bool:
    if origin not in {
        "http://jobfeed.localhost:1355",
        "https://jobfeed.localhost:1355",
    } or request.client is None:
        return False
    if any(
        len(request.headers.getlist(header)) != 1
        for header in ("origin", "host", "x-forwarded-host", "x-forwarded-proto")
    ):
        return False
    forwarded_clients = request.headers.getlist("x-forwarded-for")
    if len(forwarded_clients) > 1 or any("," in value for value in forwarded_clients):
        return False
    host = request.headers["host"]
    try:
        backend = urlsplit(f"http://{host}")
        local_backend = (
            _allowed_origin(f"http://{host}")
            and backend.hostname in {"localhost", "127.0.0.1", "::1"}
            and backend.port is not None
            and backend.port > 0
            and ip_address(request.client.host).is_loopback
        )
    except ValueError:
        return False
    public = urlsplit(origin)
    return (
        local_backend
        and request.headers["x-forwarded-host"] == public.netloc
        and request.headers["x-forwarded-proto"] == public.scheme
    )


def _mutation(request: Request) -> None:
    origin = request.headers.get("origin", "")
    direct = _allowed_origin(origin) and urlsplit(origin).netloc == request.headers.get(
        "host", ""
    )
    # Portless drops HTTP/2 :authority while forwarding to its loopback backend.
    if not direct and not _local_dashboard_proxy(request, origin):
        raise ApiError(
            403, "profile_origin", "Save your profile from this local dashboard."
        )


def _state_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/my-profile", response_model=MyProfileState)
def get_my_profile(request: Request, response: Response) -> MyProfileState:
    """Return personal fields without credentials, notes or absolute paths.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The current My Profile state.

    Raises:
        ApiError: If the profile changed underneath the read or cannot be read.
    """
    _state_headers(response)
    try:
        return _service(request).state()
    except ProfileConflict as exc:
        raise ApiError(409, "profile_conflict", str(exc)) from None
    except (OSError, ValueError, TypeError):
        raise ApiError(
            422,
            "profile_unavailable",
            "Your local profile could not be read. "
            "Check the profile and résumé catalog files.",
        ) from None


@router.put("/my-profile/personal", response_model=MyProfileState)
def save_personal(
    body: PersonalDetailsUpdate, request: Request, response: Response
) -> MyProfileState:
    """Save only the allowlisted personal fields after a concurrency check.

    Args:
        body: Personal details and the revision they were edited from.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The saved My Profile state.

    Raises:
        ApiError: If the revision is stale or the profile cannot be saved.
    """
    _mutation(request)
    _state_headers(response)
    try:
        return _service(request).save_personal(body.personal_details, body.revision)
    except ProfileConflict as exc:
        raise ApiError(409, "profile_conflict", str(exc)) from None
    except (OSError, ValueError, TypeError):
        raise ApiError(
            422,
            "profile_save_failed",
            "Your personal details could not be saved. "
            "Reload My Profile and try again.",
        ) from None


@router.post("/my-profile/application-resume", response_model=MyProfileState)
def add_application_resume(
    body: AddCurrentResume, request: Request, response: Response
) -> MyProfileState:
    """Add the current upload to the application catalog with explicit consent.

    Args:
        body: Consent payload; its content is not otherwise used.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The updated My Profile state.

    Raises:
        ApiError: If the current upload cannot be added to the catalog.
    """
    del body
    _mutation(request)
    _state_headers(response)
    try:
        return _service(request).add_current_resume()
    except (OSError, ValueError, TypeError):
        raise ApiError(
            422,
            "application_resume_failed",
            "The current upload could not be added. "
            "Upload a PDF and check the application résumé catalog.",
        ) from None


@router.put(
    "/my-profile/application-resumes/{identifier}", response_model=MyProfileState
)
def set_application_resume(
    identifier: str,
    body: ResumeAvailabilityUpdate,
    request: Request,
    response: Response,
) -> MyProfileState:
    """Enable or disable one existing PDF for future authorized batches.

    Args:
        identifier: Catalog identifier of the résumé.
        body: Whether the résumé should be available.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The updated My Profile state.

    Raises:
        ApiError: If the résumé cannot be updated.
    """
    _mutation(request)
    _state_headers(response)
    try:
        return _service(request).set_available(identifier, body.available)
    except (OSError, ValueError, TypeError):
        raise ApiError(
            422,
            "application_resume_failed",
            "This résumé could not be updated. "
            "Reload My Profile and check that its PDF is available.",
        ) from None


@router.put('/my-profile/resumes/{identifier}', response_model=MyProfileState)
def set_resume_active(identifier: str, body: ResumeActiveUpdate, request: Request, response: Response) -> MyProfileState:
    """Change one uploaded filename's active status.

    Args:
        identifier: Library identifier of the uploaded résumé.
        body: Whether the résumé should be active.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The updated My Profile state.

    Raises:
        ApiError: If the résumé cannot be updated.
    """
    _mutation(request)
    _state_headers(response)
    try:
        return _service(request).set_active(identifier,body.active)
    except (OSError,ValueError,TypeError):
        raise ApiError(422,'resume_update_failed','This resume could not be updated. Reload My Profile and try again.') from None


@router.post('/my-profile/resumes/upload', response_model=MyProfileState)
async def upload_library_resume(request: Request, response: Response,
                                file: UploadFile = File(...), metadata: str = Form(...),
                                identifier: str | None = Form(None)) -> MyProfileState:
    """Store an uploaded résumé in the library with its declared metadata.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.
        file: Uploaded résumé file.
        metadata: JSON-encoded `ResumeUploadMetadata` for the upload.
        identifier: Existing library identifier to replace, if any.

    Returns:
        The updated My Profile state.

    Raises:
        ValueError: Internally, when the metadata claims an observed Google Docs
            export, which only the local agent may register.
        ApiError: If the metadata is invalid or the file cannot be saved.
    """
    _mutation(request)
    _state_headers(response)
    try:
        details = ResumeUploadMetadata.model_validate_json(metadata)
        if details.provenance == 'observed_google_docs_export':
            raise ValueError('Browser uploads can declare an export; observed exports must be registered by the local agent')
        profile = _service(request)
        request.app.state.onboarding_resume_service.upload(
            file.filename or 'resume', await file.read(),
            library_metadata=profile.upload_metadata(), resume_metadata=details,
            identifier=identifier,
        )
        return profile.state()
    except (OSError, ValueError, TypeError) as exc:
        raise ApiError(422, 'resume_upload_failed', str(exc) if isinstance(exc, ValueError) else 'The resume could not be saved') from None


@router.put('/my-profile/resumes/{identifier}/details', response_model=MyProfileState)
def update_resume_details(identifier: str, body: ResumeUploadMetadata,
                          request: Request, response: Response) -> MyProfileState:
    """Replace the stored metadata of one library résumé.

    Args:
        identifier: Library identifier of the résumé.
        body: New metadata for the résumé.
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The updated My Profile state.

    Raises:
        ApiError: If the metadata is invalid or cannot be saved.
    """
    _mutation(request)
    _state_headers(response)
    try:
        return _service(request).update_resume(identifier, body)
    except (OSError, ValueError, TypeError) as exc:
        raise ApiError(422, 'resume_update_failed', str(exc) if isinstance(exc, ValueError) else 'The resume could not be saved') from None


@router.get('/my-profile/resumes/{identifier}/download')
def download_resume(identifier: str, request: Request) -> FileResponse:
    """Serve one library résumé file without caching.

    Args:
        identifier: Library identifier of the résumé.
        request: Incoming request carrying the app state.

    Returns:
        The résumé file under its stored filename.

    Raises:
        ApiError: If the file is unavailable.
    """
    try:
        path, filename = _service(request).download_resume(identifier)
        return FileResponse(path, filename=filename, headers={'Cache-Control': 'no-store'})
    except (OSError, ValueError, TypeError):
        raise ApiError(404, 'resume_unavailable', 'The resume file is unavailable') from None
