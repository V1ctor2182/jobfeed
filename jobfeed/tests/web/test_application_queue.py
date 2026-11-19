"""Synthetic HTTP admission preserves canonical identity and durable request keys."""

import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.application_queue import ApplicationQueueError, ApplicationQueueService
from jobfeed.services.application_queue_models import ApplicationQueueRequest
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.application_queue import router

ORIGIN = "http://127.0.0.1:4654"
PUBLIC = "https://jobfeed.localhost:1355"
OK, ACCEPTED, FORBIDDEN, INVALID, CONFLICT = 200, 202, 403, 422, 409
FALLBACK_SOURCE_ID = 888
DUPLICATE_SOURCE_ID = 403


class Store:
    def __init__(self):
        self.views = {
            "91": {
                "row": {"id": 401, "real_job_id": 91, "status": "shortlisted"},
                "sources": [{"job_id": 401, "url": "https://employer.test/apply"}],
            },
            "92": {
                "row": {"id": 402, "real_job_id": 92, "status": "applied"},
                "sources": [{"job_id": 402, "url": "https://employer.test/apply"}],
            },
        }

    async def get_real_job_view(self, real_job_id):
        return self.views.get(real_job_id)

    async def resolve_real_job_id(self, source_job_id):
        return next(
            (
                key
                for key, view in self.views.items()
                if any(str(item["job_id"]) == source_job_id for item in view["sources"])
            ),
            None,
        )


class Queue(ApplicationQueueService):
    def __init__(self, root, store):
        super().__init__(root, root / "synthetic.sqlite", store)
        self.calls = []
        self.requests = {}

    async def _command(self, command, payload):
        self.calls.append((command, payload))
        if command == "handoff" and payload["runtime"] != "claude":
            raise ApplicationQueueError(
                "invalid", "runtime must be claude: the Codex runtime was removed"
            )
        if command in {"enqueue", "handoff"}:
            receipt = {
                "request_id": payload["request_id"],
                "intent": "queue" if command == "enqueue" else "handoff",
                "status": "queued",
                "items": [
                    {**item, "status": "queued", "reason": None}
                    for item in payload["items"]
                ]
                + [
                    {**item, "status": "rejected"} for item in payload["rejected_items"]
                ],
                "plan_id": None,
                "runtime": "claude",
                "reason": None,
                "submit_policy": "stop_before_submit",
            }
            self.requests[receipt["request_id"]] = receipt
            return receipt
        return {
            "requests": list(self.requests.values()),
            "consumer": {
                "state": "active" if self.requests else "idle",
                "reason": None,
            },
        }


def app_fixture(tmp_path):
    store = Store()
    queue = Queue(tmp_path, store)
    with sqlite3.connect(queue.jobfeed_db) as db:
        db.execute("CREATE TABLE application_queue(job_id INTEGER,state TEXT)")
        db.executemany(
            "INSERT INTO application_queue VALUES (?,?)",
            [(401, "Queued"), (402, "Submitted")],
        )
    app = FastAPI()
    app.state.application_queue = queue
    app.include_router(router, prefix="/api")
    install_error_handling(app)
    return app, queue, store


def selection(*ids):
    return {
        "request_id": str(uuid4()),
        "real_job_ids": list(ids),
    }


def handoff_selection(*ids, kind="source"):
    key = "source_job_ids" if kind == "source" else "real_job_ids"
    return {
        "request_id": str(uuid4()),
        "selection": {"kind": kind, key: list(ids)},
        "authorize_dispatch": True,
        "runtime": "claude",
    }


async def test_http_resolves_canonical_id_and_persists_partial_rejections(tmp_path):
    app, queue, store = app_fixture(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        state = await client.get("/api/application-queue")
        assert state.status_code == OK
        assert [command for command, _ in queue.calls] == ["status"]
        headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
        body = selection("91", "92", "missing", "91")
        result = await client.post("/api/application-queue", headers=headers, json=body)
        assert result.status_code == ACCEPTED, result.text
        assert result.json()["items"] == [
            {
                "real_job_id": "91",
                "source_job_id": 401,
                "status": "queued",
                "reason": None,
            },
            {
                "real_job_id": "92",
                "source_job_id": None,
                "status": "rejected",
                "reason": "Only jobs currently in Wait can be prepared.",
            },
            {
                "real_job_id": "missing",
                "source_job_id": None,
                "status": "rejected",
                "reason": "The selected canonical job ID is invalid.",
            },
        ]
        payload = queue.calls[-1][1]
        assert payload["provenance"]["kind"] == "local_dashboard"
        assert payload["provenance"]["origin"] == ORIGIN
        assert "authorize_preparation" not in payload
        assert result.json()["intent"] == "queue"
        assert result.json()["submit_policy"] == "stop_before_submit"
        store.views["91"]["row"]["status"] = "applied"
        replay = await client.post("/api/application-queue", headers=headers, json=body)
        assert replay.json() == result.json()
        assert [command for command, _ in queue.calls].count("enqueue") == 1
        body["real_job_ids"] = ["92"]
        conflict = await client.post(
            "/api/application-queue", headers=headers, json=body
        )
        assert conflict.status_code == CONFLICT


@pytest.mark.parametrize("change", ["origin", "csrf", "approval", "too_many"])
async def test_invalid_admission_never_mutates(tmp_path, change):
    app, queue, _ = app_fixture(tmp_path)
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    body = selection("91")
    expected = FORBIDDEN
    if change == "origin":
        headers["Origin"] = "https://unrelated.test"
    elif change == "csrf":
        headers["x-application-queue-csrf"] = "wrong"
    elif change == "approval":
        body["authorize_preparation"] = True
        expected = INVALID
    else:
        body["real_job_ids"] = [str(index) for index in range(101)]
        expected = INVALID
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        response = await client.post(
            "/api/application-queue", headers=headers, json=body
        )
    assert response.status_code == expected
    assert queue.calls == []


@pytest.mark.parametrize("public", [PUBLIC])
async def test_verified_proxy_preserves_browser_provenance(tmp_path, public):
    app, queue, _ = app_fixture(tmp_path)
    headers = {
        "Origin": public,
        "x-application-queue-csrf": queue.csrf_token,
        "X-Forwarded-Host": public.removeprefix("https://"),
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.1",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url=ORIGIN,
    ) as client:
        response = await client.post(
            "/api/application-queue", headers=headers, json=selection("91")
        )
    assert response.status_code == ACCEPTED, response.text
    assert queue.calls[-1][1]["provenance"]["origin"] == public


async def test_resolver_uses_open_source_when_representative_closed(tmp_path):
    _, queue, store = app_fixture(tmp_path)
    store.views["91"]["sources"] = [
        {"job_id": 401, "url": "https://old.test", "closed_at": "2026-01-01"},
        {"job_id": FALLBACK_SOURCE_ID, "apply_url": "https://employer.test/apply"},
    ]
    receipt = await queue.request(ApplicationQueueRequest(**selection("91")), ORIGIN)
    assert receipt.items[0].source_job_id == FALLBACK_SOURCE_ID


async def test_status_does_not_dispatch_empty_or_pending_queue(tmp_path):
    _, queue, _ = app_fixture(tmp_path)
    await queue.state()
    assert queue.calls == [("status", {})]
    await queue.request(ApplicationQueueRequest(**selection("91")), ORIGIN)
    queue.calls.clear()
    state = await queue.state()
    assert state.requests[0].status == "queued"
    assert queue.calls == [("status", {})]


@pytest.mark.parametrize("consumer_state", ["active", "blocked"])
async def test_global_pending_state_never_dispatches_preparation(
    tmp_path, consumer_state
):
    class OlderPending(Queue):
        async def _command(self, command, payload):
            self.calls.append((command, payload))
            return {
                "requests": [
                    {
                        "request_id": str(uuid4()),
                        "intent": "legacy_automatic",
                        "status": "ready_for_review",
                        "items": [],
                        "submit_policy": "stop_before_submit",
                    }
                    for _ in range(100)
                ],
                "consumer": {"state": consumer_state, "reason": None},
            }

    queue = OlderPending(tmp_path, Store())
    state = await queue.state()
    assert state.consumer.state == consumer_state
    assert queue.calls == [("status", {})]


async def test_cli_adapter_uses_only_named_pipeline_command(tmp_path):
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    module = tmp_path / "pipeline"
    module.mkdir()
    (module / "__init__.py").touch()
    (module / "dashboard_queue.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path('call.json').write_text(json.dumps({'args':sys.argv[1:],"
        "'payload':json.load(sys.stdin)}))\n"
        "print(json.dumps({'requests':[], "
        "'consumer':{'state':'idle','reason':None}}))\n"
    )
    queue = ApplicationQueueService(tmp_path, tmp_path / "fake.sqlite", Store())
    assert (await queue.state()).requests == []
    observed = json.loads((tmp_path / "call.json").read_text())
    assert observed == {
        "args": [
            "--root",
            str(tmp_path),
            "--jobfeed-db",
            str(tmp_path / "fake.sqlite"),
            "status",
        ],
        "payload": {},
    }


async def test_cancelled_cli_process_is_terminated(tmp_path, monkeypatch):
    _, queue, _ = app_fixture(tmp_path)
    started = asyncio.Event()
    process = SimpleNamespace(returncode=None, terminated=False)

    async def communicate(_payload):
        started.set()
        await asyncio.Future()

    def terminate():
        process.terminated = True
        process.returncode = -15

    async def wait():
        return process.returncode

    async def create(*_args, **_kwargs):
        return process

    process.communicate, process.terminate, process.wait = communicate, terminate, wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(ApplicationQueueService._command(queue, "status", {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated is True


async def test_unknown_enqueue_result_requires_status_reconciliation(tmp_path):
    class Unknown(Queue):
        async def _command(self, command, payload):
            result = await super()._command(command, payload)
            if command == "enqueue":
                raise ApplicationQueueError("unavailable", "Delivery unknown")
            return result

    queue = Unknown(tmp_path, Store())
    body = ApplicationQueueRequest(**selection("91"))
    with pytest.raises(ApplicationQueueError):
        await queue.request(body, ORIGIN)
    state = await queue.state(str(body.request_id))
    assert state.requests[0].request_id == str(body.request_id)
    assert [command for command, _ in queue.calls].count("enqueue") == 1


async def test_explicit_handoff_uses_exact_queued_source_outside_wait(tmp_path):
    app, queue, store = app_fixture(tmp_path)
    store.views["91"]["row"]["status"] = "scored"
    body = handoff_selection(401, 91, 402)
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff", headers=headers, json=body
        )
        assert result.status_code == ACCEPTED, result.text
        receipt = result.json()
        assert receipt["intent"] == "handoff"
        assert receipt["items"] == [
            {
                "real_job_id": "91",
                "source_job_id": 401,
                "status": "queued",
                "reason": None,
            },
            {
                "real_job_id": None,
                "source_job_id": 91,
                "status": "rejected",
                "reason": "The selected source posting no longer exists.",
            },
            {
                "real_job_id": "92",
                "source_job_id": 402,
                "status": "rejected",
                "reason": "The selected job is no longer eligible for preparation.",
            },
        ]
        payload = queue.calls[-1][1]
        assert payload["items"] == [{"real_job_id": "91", "source_job_id": 401}]
        assert payload["authorize_preparation"] is True
        assert payload["backend_origin"] == ORIGIN
        assert payload["project_root"] == str(tmp_path.parent)
        assert receipt["controller_thread_id"] is None
        store.views["91"]["row"]["status"] = "applied"
        replay = await client.post(
            "/api/application-queue/handoff", headers=headers, json=body
        )
        assert replay.json() == receipt
        queue.calls.clear()
        status = await client.get(
            "/api/application-queue/handoff", params={"request_id": body["request_id"]}
        )
        assert status.json() == {"request": receipt}
        assert queue.calls == [("status", {"request_id": body["request_id"]})]


async def test_handoff_passes_claude_and_the_pipeline_refuses_codex(tmp_path):
    app, queue, _ = app_fixture(tmp_path)
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    claude = handoff_selection(401)
    codex = {
        key: value for key, value in handoff_selection(401).items() if key != "runtime"
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff", headers=headers, json=codex
        )
        assert result.status_code == INVALID, result.text
        assert queue.calls[-1][1]["runtime"] == "codex"
        result = await client.post(
            "/api/application-queue/handoff", headers=headers, json=claude
        )
        assert result.status_code == ACCEPTED, result.text
        assert result.json()["runtime"] == "claude"
        assert queue.calls[-1][0] == "handoff"
        assert queue.calls[-1][1]["runtime"] == "claude"
        calls = len(queue.calls)
        replay = await client.post(
            "/api/application-queue/handoff",
            headers=headers,
            json=claude | {"runtime": "codex"},
        )
        assert replay.status_code == CONFLICT
        assert all(command != "handoff" for command, _ in queue.calls[calls:])
        rejected = await client.post(
            "/api/application-queue/handoff",
            headers=headers,
            json=handoff_selection(401) | {"runtime": "gemini"},
        )
        assert rejected.status_code == INVALID
        assert [c[1]["runtime"] for c in queue.calls if c[0] == "handoff"] == [
            "codex",
            "claude",
        ]


async def test_queue_replay_ignores_the_handoff_runtime(tmp_path):
    app, queue, _ = app_fixture(tmp_path)
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    body = {"request_id": str(uuid4()), "real_job_ids": ["91"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        first = await client.post("/api/application-queue", headers=headers, json=body)
        assert first.status_code == ACCEPTED, first.text
        replay = await client.post("/api/application-queue", headers=headers, json=body)
        assert replay.status_code == ACCEPTED, replay.text
        assert replay.json() == first.json()
        assert [command for command, _ in queue.calls].count("enqueue") == 1


@pytest.mark.parametrize(
    "change",
    ["authorization", "numeric_authorization", "csrf", "wrong_kind", "source_type"],
)
async def test_handoff_requires_exact_explicit_authorization(tmp_path, change):
    app, queue, _ = app_fixture(tmp_path)
    body = handoff_selection(401)
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    expected = INVALID
    if change == "authorization":
        del body["authorize_dispatch"]
    elif change == "numeric_authorization":
        body["authorize_dispatch"] = 1
    elif change == "csrf":
        headers["x-application-queue-csrf"] = "wrong"
        expected = FORBIDDEN
    elif change == "wrong_kind":
        body["selection"]["kind"] = "canonical"
    else:
        body["selection"]["source_job_ids"] = ["401"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff", headers=headers, json=body
        )
    assert result.status_code == expected
    assert queue.calls == []


async def test_queue_request_cannot_be_reused_as_dispatch_authority(tmp_path):
    app, queue, _ = app_fixture(tmp_path)
    queued = selection("91")
    headers = {"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        assert (
            await client.post("/api/application-queue", headers=headers, json=queued)
        ).status_code == ACCEPTED
        body = handoff_selection("91", kind="canonical")
        body["request_id"] = queued["request_id"]
        response = await client.post(
            "/api/application-queue/handoff", headers=headers, json=body
        )
        assert response.status_code == CONFLICT
        missing = await client.get(
            "/api/application-queue/handoff", params={"request_id": str(uuid4())}
        )
        assert missing.json() == {"request": None}
    assert all(command != "handoff" for command, _ in queue.calls)


async def test_handoff_proxy_separates_verified_backend_and_browser_origin(tmp_path):
    app, queue, _ = app_fixture(tmp_path)
    headers = {
        "Origin": PUBLIC,
        "x-application-queue-csrf": queue.csrf_token,
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.1",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url=ORIGIN,
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff",
            headers=headers,
            json=handoff_selection(401),
        )
    assert result.status_code == ACCEPTED
    payload = queue.calls[-1][1]
    assert payload["provenance"]["origin"] == PUBLIC
    assert payload["backend_origin"] == ORIGIN


async def test_duplicate_canonical_sources_are_not_dispatched_twice(tmp_path):
    app, queue, store = app_fixture(tmp_path)
    store.views["91"]["sources"].append(
        {"job_id": 403, "url": "https://employer.test/another-posting"}
    )
    with sqlite3.connect(queue.jobfeed_db) as db:
        db.execute("INSERT INTO application_queue VALUES (403,'Queued')")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff",
            headers={"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token},
            json=handoff_selection(401, 403),
        )
    assert result.status_code == ACCEPTED
    assert queue.calls[-1][1]["items"] == [{"real_job_id": "91", "source_job_id": 401}]
    assert result.json()["items"][1]["status"] == "rejected"
    assert result.json()["items"][1]["source_job_id"] == DUPLICATE_SOURCE_ID


async def test_handoff_requires_current_queued_source_state(tmp_path):
    app, queue, _ = app_fixture(tmp_path)
    with sqlite3.connect(queue.jobfeed_db) as db:
        db.execute("UPDATE application_queue SET state='Applying' WHERE job_id=401")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/application-queue/handoff",
            headers={"Origin": ORIGIN, "x-application-queue-csrf": queue.csrf_token},
            json=handoff_selection(401),
        )
    assert result.status_code == ACCEPTED
    assert queue.calls[-1][1]["items"] == []
    assert (
        result.json()["items"][0]["reason"]
        == "The selected source is no longer queued."
    )
