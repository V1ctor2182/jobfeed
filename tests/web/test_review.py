"""The dashboard's Review page is a human reply boundary, like Discord's buttons."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.review import ReviewError, ReviewIgnore, ReviewReply, ReviewService, ReviewState
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.review import router

ORIGIN = "http://127.0.0.1:8765"
ITEM = {
    "id": "r1", "job_id": 7, "kind": "question", "text": "What postal code?", "field": "Postal Code",
    "options": ["11101", "11101-2910"], "quick_replies": [], "created_at": "2026-11-11T06:30:50+00:00",
}
REPLY = {"request_id": "r1", "action": "pick", "text": "11101", "click_id": "click-0001"}
IGNORE = {"job_id": 7, "click_id": "click-0002"}
RESUME = {"site": "indeed", "click_id": "click-0003"}


class FakeReview:
    def __init__(self):
        self.writes = []
        self.failure = None

    def state(self):
        return ReviewState.model_validate({"items": [ITEM]})

    def reply(self, body):
        return self._write(body)

    def ignore(self, body):
        return self._write(body)

    def resume_site(self, body):
        return self._write(body)

    def _write(self, body):
        self.writes.append(body)
        if self.failure:
            raise self.failure
        return ReviewState(items=[])


def app(service):
    application = FastAPI()
    install_error_handling(application)
    application.state.review_service = service
    application.include_router(router, prefix="/api")
    return application


def client(service):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app(service)), base_url=ORIGIN)


async def test_open_questions_are_listed_without_caching():
    async with client(FakeReview()) as http:
        result = await http.get("/api/review")
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["items"][0]["options"] == ["11101", "11101-2910"]


@pytest.mark.parametrize("path,payload", [("/api/review/reply", REPLY), ("/api/review/ignore", IGNORE), ("/api/review/resume-site", RESUME)])
async def test_only_the_dashboards_own_origin_can_reply_or_ignore(path, payload):
    service = FakeReview()
    async with client(service) as http:
        for headers in ({}, {"Origin": "https://attacker.test"}, {"Origin": "http://localhost:8765"}):
            assert (await http.post(path, json=payload, headers=headers)).status_code == 403
        assert service.writes == []
        saved = await http.post(path, json=payload, headers={"Origin": ORIGIN})
    assert (saved.status_code, saved.json()) == (200, {"items": [], "sites": [], "ended": 0})
    assert len(service.writes) == 1


async def test_a_reply_cannot_name_its_own_sender_or_source():
    service = FakeReview()
    async with client(service) as http:
        result = await http.post("/api/review/reply", json={**REPLY, "sender_id": "someone"}, headers={"Origin": ORIGIN})
    assert result.status_code == 422
    assert service.writes == []


@pytest.mark.parametrize("code,status", [("invalid", 422), ("unavailable", 503)])
async def test_a_rejected_reply_reports_why(code, status):
    service = FakeReview()
    service.failure = ReviewError(code, "This question is already closed.")
    async with client(service) as http:
        result = await http.post("/api/review/reply", json=REPLY, headers={"Origin": ORIGIN})
    assert result.status_code == status
    assert result.json()["error"]["code"] == f"review_{code}"


def test_the_adapter_runs_the_pipeline_program_with_fixed_arguments(monkeypatch, tmp_path: Path):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps({"items": [ITEM]}), "")

    monkeypatch.setattr(subprocess, "run", run)
    service = ReviewService(tmp_path, tmp_path / "jobfeed.db")
    assert service.state().items[0].id == "r1"
    service.reply(ReviewReply(**REPLY))
    service.ignore(ReviewIgnore(**IGNORE))
    base = [str(tmp_path / ".venv/bin/python"), "-m", "pipeline.dashboard_review", "--root", str(tmp_path), "--jobfeed-db", str(tmp_path / "jobfeed.db")]
    assert [call[0] for call in calls] == [[*base, "list"], [*base, "reply"], [*base, "ignore"]]
    assert json.loads(calls[1][1]["input"]) == REPLY


def test_a_pipeline_refusal_becomes_an_invalid_reply(monkeypatch, tmp_path: Path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, json.dumps({"code": "invalid", "error": "This question is already closed."}), "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ReviewError) as caught:
        ReviewService(tmp_path, tmp_path / "jobfeed.db").reply(ReviewReply(**REPLY))
    assert (caught.value.code, str(caught.value)) == ("invalid", "This question is already closed.")


async def test_a_site_name_that_is_not_a_plain_identifier_is_refused():
    service = FakeReview()
    async with client(service) as http:
        result = await http.post("/api/review/resume-site", json={**RESUME, "site": "indeed; rm"}, headers={"Origin": ORIGIN})
    assert result.status_code == 422
    assert service.writes == []
