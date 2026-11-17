"""The tracker route only reads what the pipeline saved."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.tracker import TrackerBody, TrackerError, TrackerNotFound, TrackerService, TrackerState
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.tracker import router

STATE = {
    "messages": [{
        "id": "m1", "thread_id": "m1", "sender": "no-reply@ashbyhq.com", "sender_name": "Realm Hiring Team",
        "subject": "Realm Alliance Application Update", "snippet": "we decided to move forward with other candidates",
        "received_at": "2026-11-11T14:02:26+00:00", "status": "rejected", "company": "Realm", "job_ids": [7],
    }],
    "applications": [{
        "job_id": 7, "company": "Realm", "title": "Software Engineer", "source": "jobfeed", "stage": "rejected",
        "confirmed_by_email": True, "email_count": 2, "applied_at": "2026-11-05T00:00:00Z", "last_activity_at": "2026-11-11T14:02:26+00:00",
    }],
    "last_sync": {"finished_at": "2026-11-11T15:00:00+00:00", "new_messages": 3, "error": None},
}


BODY = {
    "id": "1a1cadc0e814c0ae", "sender": "kyndryl@myworkday.com", "subject": "Update on your application", "received_at": "2026-11-11T13:46:20+00:00",
    "status": "rejected", "company": "Kyndryl", "body": "Dear Alex,\n\nWe regret to inform you.", "recipients": ["me@example.com"], "fetched_at": "2026-11-11T18:00:00+00:00",
}


class FakeTracker:
    failure = None

    def body(self, message_id):
        if self.failure:
            raise self.failure
        return TrackerBody.model_validate({**BODY, "id": message_id})

    def state(self):
        if self.failure:
            raise self.failure
        return TrackerState.model_validate(STATE)


def client(service):
    application = FastAPI()
    install_error_handling(application)
    application.state.tracker_service = service
    application.include_router(router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1:8765")


async def test_the_tracker_returns_mail_and_stages_without_caching():
    async with client(FakeTracker()) as http:
        result = await http.get("/api/tracker")
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["applications"][0]["stage"] == "rejected"
    assert result.json()["messages"][0]["job_ids"] == [7]


async def test_an_unreadable_tracker_is_unavailable():
    service = FakeTracker()
    service.failure = TrackerError("Application email could not be read.")
    async with client(service) as http:
        result = await http.get("/api/tracker")
    assert (result.status_code, result.json()["error"]["code"]) == (503, "tracker_unavailable")


async def test_the_route_has_no_write_method():
    async with client(FakeTracker()) as http:
        assert (await http.post("/api/tracker", json={})).status_code == 405


async def test_a_saved_message_opens_with_its_body():
    async with client(FakeTracker()) as http:
        result = await http.get("/api/tracker/messages/1a1cadc0e814c0ae")
    assert result.json()["body"].startswith("Dear Alex")
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("message_id", ["../etc", "--root", "XYZ", "1a"])
async def test_a_message_id_that_is_not_a_gmail_id_never_reaches_the_pipeline(message_id):
    service = FakeTracker()
    service.failure = AssertionError("the pipeline was asked")
    async with client(service) as http:
        assert (await http.get(f"/api/tracker/messages/{message_id}")).status_code in (404, 422)


async def test_an_unsaved_message_is_not_found_and_a_gmail_failure_is_unavailable():
    service = FakeTracker()
    async with client(service) as http:
        service.failure = TrackerNotFound("unknown message")
        assert (await http.get("/api/tracker/messages/1a1cadc0e814c0ae")).status_code == 404
        service.failure = TrackerError("This email could not be fetched.")
        assert (await http.get("/api/tracker/messages/1a1cadc0e814c0ae")).status_code == 503


def test_the_body_adapter_passes_the_id_as_one_argument(monkeypatch, tmp_path: Path):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(BODY), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert TrackerService(tmp_path, tmp_path / "jobfeed.db").body("1a1cadc0e814c0ae").company == "Kyndryl"
    assert calls[0][-3:] == ["body", "--id", "1a1cadc0e814c0ae"]


def test_a_pipeline_refusal_is_not_found(monkeypatch, tmp_path: Path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, json.dumps({"error": "unknown message"}), "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TrackerNotFound):
        TrackerService(tmp_path, tmp_path / "jobfeed.db").body("1a1cadc0e814c0ae")


def test_the_adapter_runs_the_pipeline_view_with_fixed_arguments(monkeypatch, tmp_path: Path):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(STATE), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert TrackerService(tmp_path, tmp_path / "jobfeed.db").state().last_sync.new_messages == 3
    assert calls == [[str(tmp_path / ".venv/bin/python"), "-m", "pipeline.mail_tracker", "--root", str(tmp_path), "--jobfeed-db", str(tmp_path / "jobfeed.db"), "view"]]


def test_a_failed_pipeline_run_is_a_tracker_error(monkeypatch, tmp_path: Path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "boom")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TrackerError):
        TrackerService(tmp_path, tmp_path / "jobfeed.db").state()
