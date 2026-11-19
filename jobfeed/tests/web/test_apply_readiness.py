from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest

from jobfeed.pipeline_readiness import PipelineReadiness
from jobfeed.services.readiness_models import ReadinessRun
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import fake_context, open_client

ORIGIN = "http://127.0.0.1:7654"
RUN_ID = "12345678-1234-4234-8234-123456789abc"


def run_result() -> ReadinessRun:
    return ReadinessRun(
        run_id=RUN_ID,
        phase="blocked",
        started_at="2026-10-21T12:00:00+00:00",
        finished_at=None,
        expires_at=None,
        stale=False,
        ready=False,
        checks=[
            {
                "id": "registration",
                "label": "Diagnostic relay and browser binding",
                "status": "blocked",
                "required": True,
                "checked_at": None,
                "reason": "Configure the dedicated diagnostic relay.",
                "next_step": "Run the local registration command.",
            }
        ],
    )


class FakeReadiness:
    def __init__(self):
        self.starts = []
        self.observed = None

    def start(self, request_id, origin):
        self.starts.append((request_id, origin))
        self.observed = run_result()
        return self.observed

    def get(self):
        return self.observed

    def fixture(self, run_id):
        if run_id != RUN_ID:
            raise ValueError("private path and secret")
        return '<html><input type="file" accept="application/pdf"></html>'


@pytest.fixture
def fixture_app():
    app = build_web_app(fake_context(), static_dir=Path("/does-not-exist"))
    provider = FakeReadiness()
    app.state.apply_readiness = provider
    return app, provider


async def test_manual_button_starts_once_and_get_restores_without_starting(fixture_app):
    app, provider = fixture_app
    request_id = str(uuid.uuid4())
    async with open_client(app) as client:
        initial = await client.get("/api/apply-readiness")
        assert initial.status_code == HTTPStatus.OK
        assert initial.json()["run"] is None
        token = initial.json()["csrf_token"]
        headers = {
            "Host": "127.0.0.1:7654",
            "Origin": ORIGIN,
            "X-Readiness-CSRF": token,
        }
        result = await client.post(
            "/api/apply-readiness", headers=headers, json={"request_id": request_id}
        )
        assert result.status_code == HTTPStatus.OK
        assert result.json() == run_result().model_dump()
        restored = await client.get("/api/apply-readiness")
        assert restored.json()["run"] == result.json()
        assert provider.starts == [(request_id, ORIGIN)]


@pytest.mark.parametrize(
    "changed,expected",
    [
        ({"Origin": None}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "http://127.0.0.1:7654.evil.example"}, 403),
        ({"Origin": ORIGIN + "/"}, 403),
        ({"Host": "evil.example", "Origin": "http://evil.example"}, 403),
        ({"X-Readiness-CSRF": "wrong"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Transfer-Encoding": "chunked"}, 415),
        ({"Content-Length": "1024"}, 413),
    ],
)
async def test_invalid_mutation_never_admits_work(fixture_app, changed, expected):
    app, provider = fixture_app
    async with open_client(app) as client:
        token = (await client.get("/api/apply-readiness")).json()["csrf_token"]
        headers = {
            "Host": "127.0.0.1:7654",
            "Origin": ORIGIN,
            "X-Readiness-CSRF": token,
            "Content-Type": "application/json",
        }
        for key, value in changed.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        response = await client.post(
            "/api/apply-readiness",
            headers=headers,
            content=json.dumps({"request_id": str(uuid.uuid4())}),
        )
        assert response.status_code == expected
        assert provider.starts == []
        assert (await client.get("/api/apply-readiness")).json()["run"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"request_id": "bad"},
        {"request_id": None},
        {"request_id": RUN_ID, "url": "https://employer.example"},
        {"request_id": RUN_ID, "command": "worker start"},
    ],
)
async def test_strict_body_rejects_arbitrary_targets(fixture_app, body):
    app, provider = fixture_app
    async with open_client(app) as client:
        token = (await client.get("/api/apply-readiness")).json()["csrf_token"]
        response = await client.post(
            "/api/apply-readiness",
            json=body,
            headers={
                "Host": "127.0.0.1:7654",
                "Origin": ORIGIN,
                "X-Readiness-CSRF": token,
            },
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert provider.starts == []
        assert (await client.get("/api/apply-readiness")).json()["run"] is None


async def test_fixture_has_no_network_or_form_destination(fixture_app):
    app, provider = fixture_app
    async with open_client(app) as client:
        response = await client.get(f"/api/apply-readiness/fixture/{RUN_ID}")
        assert response.status_code == HTTPStatus.OK
        assert "form-action 'none'" in response.headers["content-security-policy"]
        assert "connect-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert (
            response.text == '<html><input type="file" accept="application/pdf"></html>'
        )
        unknown = await client.get("/api/apply-readiness/fixture/not-known")
        assert unknown.status_code == HTTPStatus.NOT_FOUND
        assert "private path" not in unknown.text
        assert provider.starts == []


async def test_runtime_errors_are_redacted_before_http_response(fixture_app):
    app, provider = fixture_app
    async with open_client(app) as client:
        token = (await client.get("/api/apply-readiness")).json()["csrf_token"]
        with patch.object(
            provider,
            "start",
            side_effect=RuntimeError("PRIVATE_PROFILE /private/resume.pdf SECRET"),
        ):
            response = await client.post(
                "/api/apply-readiness",
                json={"request_id": RUN_ID},
                headers={
                    "Host": "127.0.0.1:7654",
                    "Origin": ORIGIN,
                    "X-Readiness-CSRF": token,
                },
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert (
            response.json()["error"]["message"]
            == "Readiness setup is unavailable. Check the local runtime configuration."
        )
        assert "PRIVATE_PROFILE" not in response.text
        assert "resume.pdf" not in response.text
        assert provider.starts == []


def test_real_pipeline_subprocess_uses_explicit_root_and_database(tmp_path):
    source = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    if not source.is_dir():
        pytest.skip("needs resume-application-pipeline as a sibling checkout")
    root = tmp_path / "application-runtime"
    shutil.copytree(
        source / "pipeline",
        root / "pipeline",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    database = tmp_path / "explicit-jobfeed.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE jobs(id,real_job_id,company,title,jd_text,url); "
            "CREATE TABLE applied(job_id); CREATE TABLE application_queue("
            "job_id,state,retry_count,next_retry_at,application_folder_relpath,"
            "codex_application_session_id,submit_authorization);"
        )
    before = database.read_bytes()
    provider = PipelineReadiness(root, database)
    assert provider.get() is None
    run = provider.start(str(uuid.uuid4()), ORIGIN)
    assert run.phase == "blocked"
    checks = {item.id: item for item in run.checks}
    assert checks["jobfeed_db"].status == "ready"
    assert checks["python"].status == "ready"
    assert checks["jobfeed_cli"].status == "ready"
    assert run.finished_at is not None
    assert run.expires_at is not None
    assert checks["profile"].status == "blocked"
    assert checks["resumes"].status == "blocked"
    assert checks["registration"].status == "blocked"
    assert checks["chrome"].status == "not_tested"
    assert provider.get() == run
    assert database.read_bytes() == before
    assert not (root / ".pipeline/application-readiness-bridge.sqlite3").exists()
    assert not (root / "applications").exists()
    assert str(root) not in run.model_dump_json()
    assert str(database) not in run.model_dump_json()


def test_missing_source_returns_component_results_without_creating_root(tmp_path):
    root = tmp_path / "missing-source"
    provider = PipelineReadiness(root, tmp_path / "absent-db.sqlite")
    run = provider.start(RUN_ID, ORIGIN)
    assert run.phase == "blocked"
    checks = {item.id: item for item in run.checks}
    assert checks["runtime_root"].status == "blocked"
    assert checks["profile"].status == "not_tested"
    assert checks["resumes"].status == "not_tested"
    assert checks["chrome"].status == "not_tested"
    assert checks["upload"].status == "not_tested"
    assert not run.ready
    assert provider.get() == run
    assert not root.exists()
