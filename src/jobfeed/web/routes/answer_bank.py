"""Local dashboard saving and approval of application answers. Saving an answer here approves it."""

from typing import cast

from fastapi import APIRouter, Request, Response

from jobfeed.answer_bank import (
    AnswerBankError,
    AnswerBankMutation,
    AnswerBankService,
    AnswerBankState,
)
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError

router = APIRouter()


def _service(request: Request) -> AnswerBankService:
    return cast(AnswerBankService, request.app.state.answer_bank_service)


def _error(exc: AnswerBankError) -> ApiError:
    status = {"conflict": 409, "invalid": 422, "unavailable": 503}[exc.code]
    return ApiError(status, f"answer_bank_{exc.code}", str(exc))


@router.get("/answer-bank", response_model=AnswerBankState)
def get_answer_bank(request: Request, response: Response) -> AnswerBankState:
    """Return the current answer bank.

    Args:
        request: Incoming request carrying the app state.
        response: Response whose cache headers are set.

    Returns:
        The answer bank state.

    Raises:
        ApiError: If the answer bank cannot be read.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).state()
    except AnswerBankError as exc:
        raise _error(exc) from None


@router.post("/answer-bank", response_model=AnswerBankState)
def mutate_answer_bank(
    body: AnswerBankMutation, request: Request, response: Response
) -> AnswerBankState:
    """Apply one change to the answer bank from the local dashboard.

    Args:
        body: The change and the revision it expects.
        request: Incoming request; it needs exactly one loopback origin and host.
        response: Response whose cache headers are set.

    Returns:
        The answer bank state after the change.

    Raises:
        ApiError: If the request is not from the local dashboard or the change
            is rejected.
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
            403, "answer_bank_origin", "Review answers from this local dashboard."
        )
    response.headers["Cache-Control"] = "no-store"
    try:
        return _service(request).mutate(body)
    except AnswerBankError as exc:
        raise _error(exc) from None
