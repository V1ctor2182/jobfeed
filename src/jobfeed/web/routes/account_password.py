"""Set the shared account password from the dashboard. Nothing here returns it."""

from typing import cast

from fastapi import APIRouter, Request, Response

from jobfeed.account_password import (
    AccountPasswordError,
    AccountPasswordService,
    AccountPasswordState,
    AccountPasswordUpdate,
)
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError

router = APIRouter()


def _service(request: Request) -> AccountPasswordService:
    return cast(AccountPasswordService, request.app.state.account_password_service)


def _error(exc: AccountPasswordError) -> ApiError:
    status = {"invalid": 422, "unavailable": 503}[exc.code]
    return ApiError(status, f"account_password_{exc.code}", str(exc))


@router.get("/my-profile/account-password", response_model=AccountPasswordState)
def get_account_password(request: Request, response: Response) -> AccountPasswordState:
    """Report whether a shared account password is set.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        Whether a password is set and how many accounts use it. Never the password.

    Raises:
        ApiError: If the status cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).state()
    except AccountPasswordError as exc:
        raise _error(exc) from None


@router.put("/my-profile/account-password", response_model=AccountPasswordState)
def put_account_password(
    body: AccountPasswordUpdate, request: Request, response: Response
) -> AccountPasswordState:
    """Save a new shared account password typed by the user.

    Args:
        body: The new password.
        request: Incoming request; it must come from the dashboard's own origin.
        response: Response whose cache headers are set.

    Returns:
        The status after saving. Never the password.

    Raises:
        ApiError: If the request is not from the dashboard or the password is refused.
    """
    expected = dashboard_loopback_origin(
        request, browser_origin=request.headers.get("origin", "")
    )
    if (
        expected is None
        or len(request.headers.getlist("origin")) != 1
        or len(request.headers.getlist("host")) != 1
    ):
        raise ApiError(
            403, "account_password_origin", "Set the password from this dashboard."
        )
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).save(body)
    except AccountPasswordError as exc:
        raise _error(exc) from None
