"""Real HTTP, subprocess adapter and pipeline admission with external Codex faked."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfeed.application_reruns import ApplicationRerunService
from jobfeed.services.jobright_bridge import JobrightBridge
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.application_reruns import router

PIPELINE = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
pytestmark = pytest.mark.skipif(
    not PIPELINE.is_dir(), reason="needs resume-application-pipeline as a sibling checkout"
)
ORIGIN = "https://jobfeed.localhost:1355"


@pytest.fixture
def rerun_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(PIPELINE))
    base_spec = importlib.util.spec_from_file_location(
        "tests.rerun_fixture", PIPELINE / "tests/rerun_fixture.py"
    )
    base_module = importlib.util.module_from_spec(base_spec)
    base_spec.loader.exec_module(base_module)
    monkeypatch.setitem(sys.modules, "tests.rerun_fixture", base_module)
    spec = importlib.util.spec_from_file_location(
        "restart_fixture", PIPELINE / "tests/restart_fixture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.RestartFixture(tmp_path)
    python = fixture.root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    thread = tmp_path / "thread.json"
    thread.write_text(json.dumps(fixture.thread))
    calls = tmp_path / "calls.jsonl"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        """
import json
from pathlib import Path
import subprocess
import pipeline.codex_app_server as server
class ExternalCodex:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def request(self, method, params):
        assert method == 'thread/read'
        return {'thread': json.loads(Path(THREAD).read_text())}
server.CodexAppServer = ExternalCodex
original_run = subprocess.run
def external_queue(argv, **kwargs):
    if len(argv) > 1 and argv[1] == 'queue':
        with Path(CALLS).open('a') as stream:
            stream.write(json.dumps(argv) + '\\n')
        return subprocess.CompletedProcess(
            argv, 0, 'Queued message fake-one for thread owner\\n', '')
    return original_run(argv, **kwargs)
subprocess.run = external_queue
""".replace("THREAD", repr(str(thread))).replace("CALLS", repr(str(calls)))
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(PIPELINE)]))
    bridge = JobrightBridge()
    bridge.connect(["application-tab-pair-v1"])

    class ExtensionState(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/application-tabs"
            payload = json.dumps({"profile_probe_available": bridge.connected and "application-tab-pair-v1" in bridge.supported_sources,
                                  "profile_probe_reason": "Reload the Jobfeed extension."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    dashboard = ThreadingHTTPServer(("127.0.0.1", 0), ExtensionState)
    server = Thread(target=dashboard.serve_forever, daemon=True)
    server.start()
    app = FastAPI()
    app.state.context = {"jobright_bridge": bridge}

    @app.middleware("http")
    async def bound_server(request, call_next):
        request.scope["server"] = dashboard.server_address
        return await call_next(request)
    app.state.application_reruns = ApplicationRerunService(fixture.root, fixture.db)
    app.include_router(router, prefix="/api")
    install_error_handling(app)
    yield app, fixture, thread, calls
    dashboard.shutdown()
    dashboard.server_close()
    server.join()


def test_http_to_real_cli_queues_restart_once_and_observes_relay(rerun_app):
    app, fixture, thread, calls = rerun_app
    with TestClient(app, base_url=ORIGIN) as client:
        state = client.get(
            "/api/application-reruns", params={"batch_id": "test", "job_id": 1}
        )
        assert state.status_code == HTTPStatus.OK
        assert state.json()["eligible"] is True
        assert not calls.exists()
        headers = {
            "Origin": ORIGIN,
            "x-application-reruns-csrf": state.json()["csrf_token"],
        }
        body = {
            "batch_id": "test",
            "job_id": 1,
            "request_id": str(uuid4()),
            "expected_owner": "owner",
        }
        result = client.post("/api/application-reruns", headers=headers, json=body)
        assert result.status_code == HTTPStatus.ACCEPTED, result.text
        assert result.json()["request"]["state"] == "queued"
        assert result.json()["request"]["phase"] == "relay_pending"
        assert result.json()["request"]["replacement_thread_id"] is None
        assert (
            client.post("/api/application-reruns", headers=headers, json=body).json()[
                "request"
            ]["state"]
            == "queued"
        )
        body["request_id"] = str(uuid4())
        assert (
            client.post("/api/application-reruns", headers=headers, json=body).json()[
                "request"
            ]["state"]
            == "queued"
        )
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        assert len(commands) == 1
        assert commands[0][1:4] == ["queue", "--thread", "owner"]
        prompt = commands[0][-1]
        assert "STOP BEFORE SUBMIT" in prompt
        assert "closing its previous form and replacing this applier chat" in prompt
        assert "archive this old applier chat" in prompt
        fixture.sent.append(("owner", prompt))
        fixture.running()
        thread.write_text(json.dumps(fixture.thread))
        running = client.get(
            "/api/application-reruns", params={"batch_id": "test", "job_id": 1}
        ).json()
        assert running["request"]["state"] == "running"
        assert running["request"]["phase"] == "cleanup"
        assert running["owner_thread_id"] == "owner"
        fixture.thread["turns"][-1]["status"] = "completed"
        fixture.thread["status"] = {"type": "idle"}
        fixture.save_lifecycle()
        thread.write_text(json.dumps(fixture.thread))
        uncertain = client.get(
            "/api/application-reruns", params={"batch_id": "test", "job_id": 1}
        ).json()
        assert uncertain["request"]["state"] == "unknown"
        assert uncertain["eligible"] is False
        assert uncertain["request"]["phase"] == "cleanup"
        assert len(calls.read_text().splitlines()) == 1
    with sqlite3.connect(fixture.root / ".pipeline/blockers.sqlite3") as db:
        assert db.execute("SELECT active FROM dq_scopes").fetchone()[0] == 0


@pytest.mark.parametrize(
    "origin,token",
    [
        (None, True),
        ("https://evil.example", True),
        ("https://other.localhost:1355", True),
        (ORIGIN, False),
    ],
)
def test_http_rejects_cross_origin_and_missing_csrf(rerun_app, origin, token):
    app, _, _, calls = rerun_app
    headers = {}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["x-application-reruns-csrf"] = app.state.application_reruns.csrf_token
    with TestClient(app, base_url=ORIGIN) as client:
        result = client.post(
            "/api/application-reruns",
            headers=headers,
            json={
                "batch_id": "test",
                "job_id": 1,
                "request_id": str(uuid4()),
                "expected_owner": "owner",
            },
        )
    assert result.status_code == HTTPStatus.FORBIDDEN
    assert not calls.exists()


def test_http_rejects_browser_prompts_paths_and_forged_owner(rerun_app):
    app, _, _, calls = rerun_app
    headers = {
        "Origin": ORIGIN,
        "x-application-reruns-csrf": app.state.application_reruns.csrf_token,
    }
    body = {
        "batch_id": "test",
        "job_id": 1,
        "request_id": str(uuid4()),
        "expected_owner": "owner",
    }
    with TestClient(app, base_url=ORIGIN) as client:
        assert (
            client.post(
                "/api/application-reruns",
                headers=headers,
                json={**body, "prompt": "submit"},
            ).status_code
            == HTTPStatus.UNPROCESSABLE_ENTITY
        )
        assert (
            client.post(
                "/api/application-reruns",
                headers=headers,
                json={**body, "batch_id": "../escape"},
            ).status_code
            == HTTPStatus.UNPROCESSABLE_ENTITY
        )
        assert (
            client.post(
                "/api/application-reruns",
                headers=headers,
                json={**body, "expected_owner": "replacement"},
            ).status_code
            == HTTPStatus.CONFLICT
        )
    assert not calls.exists()


def test_unsupported_extension_disables_rerun_before_queue(rerun_app):
    app, _, _, calls = rerun_app
    app.state.context["jobright_bridge"].supported_sources = frozenset({"jobright"})
    with TestClient(app, base_url=ORIGIN) as client:
        state = client.get("/api/application-reruns", params={"batch_id": "test", "job_id": 1})
        assert state.status_code == 200
        assert state.json()["eligible"] is False
        assert "Reload" in state.json()["reason"]
        response = client.post("/api/application-reruns", headers={"Origin": ORIGIN,
            "x-application-reruns-csrf": state.json()["csrf_token"]}, json={"batch_id": "test", "job_id": 1,
            "request_id": str(uuid4()), "expected_owner": "owner"})
        assert response.status_code == 503
        assert not calls.exists()
