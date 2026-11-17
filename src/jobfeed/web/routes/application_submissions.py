"""Human-confirmed submission commands with a same-origin CSRF boundary."""
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from jobfeed.application_submissions import ApplicationSubmissionError
from jobfeed.web.dashboard_origin import dashboard_loopback_origin
from jobfeed.web.errors import ApiError
from jobfeed.web.routes.application_tabs import _allowed_origin

class SubmissionRoute(APIRoute):
    """API route that rejects submission requests larger than 2 MB."""

    def get_route_handler(self):
        """Wrap the route handler so request bodies are counted while they stream in.

        Returns:
            A handler that raises HTTP 413 once the body exceeds 2 MB.
        """
        handler = super().get_route_handler()

        async def bounded(request: Request):
            received = 0

            async def receive():
                nonlocal received
                message = await request.receive()
                received += len(message.get('body', b''))
                if received > 2_000_000:
                    raise HTTPException(413, 'Submission review is too large.')
                return message

            return await handler(Request(request.scope, receive))
        return bounded


router = APIRouter(route_class=SubmissionRoute)


class SubmissionConfirmation(BaseModel):
    """The user's confirmation of a reviewed submission preview."""

    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=100)
    expected_preview: dict[str, Any]
    confirmed: bool


def _command(request: Request, response: Response, command: str, payload: dict) -> dict:
    service = request.app.state.application_submissions
    host = request.headers.get('host', '')
    if not _allowed_origin('http://' + host):
        raise ApiError(403, 'application_submission_origin', 'Use the local dashboard.')
    if command == 'confirm':
        origin = request.headers.get('origin', '')
        direct_origin = (
            _allowed_origin(origin)
            and urlsplit(origin).hostname in {'localhost', '127.0.0.1', '::1'}
            and urlsplit(origin).netloc == host
        )
        proxy_origin = dashboard_loopback_origin(request, browser_origin=origin)
        if (
            any(len(request.headers.getlist(header)) != 1 for header in (
                'origin', 'host', 'x-application-submissions-csrf'
            ))
            or not (direct_origin or proxy_origin is not None)
            or not secrets.compare_digest(
                request.headers.get('x-application-submissions-csrf', ''),
                service.csrf_token,
            )
        ):
            raise ApiError(403, 'application_submission_csrf', 'Confirm submission from this dashboard.')
    response.headers['Cache-Control'] = 'no-store'
    try:
        return service.command(command, payload)
    except ApplicationSubmissionError as exc:
        raise ApiError({'invalid': 422, 'conflict': 409, 'unavailable': 503}[exc.code], 'application_submission_' + exc.code, str(exc)) from None


@router.get('/application-submissions/preview')
def preview(request: Request, response: Response, batch_id: str = Query(min_length=1, max_length=200), job_id: int = Query(gt=0)) -> dict:
    """Return the submission preview for one job in a batch.

    Args:
        request: Incoming dashboard request.
        response: Response whose headers are set.
        batch_id: Batch the job belongs to.
        job_id: Job to preview.

    Returns:
        The preview reported by the pipeline.
    """
    return _command(request, response, 'preview', {'batch_id': batch_id, 'job_id': job_id})


@router.get('/application-submissions/status')
def status(request: Request, response: Response, request_id: str = Query(min_length=1, max_length=100)) -> dict:
    """Return the state of one submission request.

    Args:
        request: Incoming dashboard request.
        response: Response whose headers are set.
        request_id: Submission request to look up.

    Returns:
        The request state reported by the pipeline.
    """
    return _command(request, response, 'status', {'request_id': request_id})


@router.post('/application-submissions/confirm', status_code=202)
def confirm(body: SubmissionConfirmation, request: Request, response: Response) -> dict:
    """Pass the user's confirmation of a reviewed preview to the pipeline.

    Args:
        body: Request ID, the reviewed preview and the confirmation.
        request: Incoming dashboard mutation request.
        response: Response whose headers are set.

    Returns:
        The pipeline's response to the confirmation.
    """
    return _command(request, response, 'confirm', body.model_dump())
