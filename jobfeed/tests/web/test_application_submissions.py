"""HTTP submission admission: external origins cannot approve or release a job."""

from http import HTTPStatus
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.application_submissions import router


def client():
    app = FastAPI()
    calls = []

    def command(name, body):
        calls.append((name, body))
        return {"eligible": False, "reason": "Already submitted"}

    app.state.application_submissions = SimpleNamespace(
        csrf_token="secret", command=command
    )
    install_error_handling(app)
    app.include_router(router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1:8765"), calls


def test_confirmation_requires_same_origin_and_csrf():
    http, calls = client()
    body = {"request_id": "request", "expected_preview": {}, "confirmed": True}
    for headers in (
        {},
        {"Origin": "https://evil.example", "X-Application-Submissions-CSRF": "secret"},
        {"Origin": "http://127.0.0.1:8765"},
    ):
        assert (
            http.post(
                "/api/application-submissions/confirm", json=body, headers=headers
            ).status_code
            == HTTPStatus.FORBIDDEN
        )
    assert not calls


def test_full_review_over_eight_kib_is_forwarded_exactly():
    http, calls = client()
    body = {
        "request_id": "request",
        "expected_preview": {"answers": [{"answer": "x" * 12000}]},
        "confirmed": True,
    }
    result = http.post(
        "/api/application-submissions/confirm",
        json=body,
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Application-Submissions-CSRF": "secret",
        },
    )
    assert result.status_code == HTTPStatus.ACCEPTED
    assert calls == [("confirm", body)]
    assert result.headers["cache-control"] == "no-store"


def test_preview_uses_pipeline_eligibility():
    http, calls = client()
    result = http.get("/api/application-submissions/preview?batch_id=batch&job_id=42")
    assert result.json()["eligible"] is False
    assert calls == [("preview", {"batch_id": "batch", "job_id": 42})]
