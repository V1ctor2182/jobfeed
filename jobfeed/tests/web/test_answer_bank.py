"""The dashboard is the local human approval boundary."""

import json
import subprocess
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jobfeed.answer_bank import (
    AnswerBankError,
    AnswerBankMutation,
    AnswerBankService,
    AnswerBankState,
)
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.answer_bank import router

ORIGIN = "http://127.0.0.1:8765"
EMPTY = {"schema_version": 1, "revision": 0, "entries": []}


class FakeBank:
    def __init__(self):
        self.writes = []
        self.failure = None

    def state(self):
        return AnswerBankState.model_validate(EMPTY)

    def mutate(self, request):
        self.writes.append(request)
        if self.failure:
            raise self.failure
        return self.state()


def app(bank):
    application = FastAPI()
    install_error_handling(application)
    application.state.answer_bank_service = bank
    application.include_router(router, prefix="/api")
    return application


async def test_origin_and_provenance_boundary():
    bank = FakeBank()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(bank)), base_url=ORIGIN
    ) as client:
        state = await client.get("/api/answer-bank")
        assert state.json() == {**EMPTY, "common_questions": []}
        assert state.headers["cache-control"] == "no-store"
        payload = {
            "action": "approve",
            "entry_id": "one",
            "expected_revision": 0,
            "expected_version": 1,
        }
        for headers in (
            {},
            {"Origin": "https://attacker.test"},
            {"Origin": "http://localhost:8765"},
        ):
            result = await client.post(
                "/api/answer-bank", json=payload, headers=headers
            )
            assert result.status_code == HTTPStatus.FORBIDDEN
        forged = await client.post(
            "/api/answer-bank",
            json={**payload, "source": {"kind": "user"}},
            headers={"Origin": ORIGIN},
        )
        assert forged.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert bank.writes == []
        saved = await client.post(
            "/api/answer-bank", json=payload, headers={"Origin": ORIGIN}
        )
        assert saved.status_code == HTTPStatus.OK
        assert saved.headers["cache-control"] == "no-store"
        assert len(bank.writes) == 1


@pytest.mark.parametrize(
    "code,status", [("conflict", 409), ("invalid", 422), ("unavailable", 503)]
)
async def test_failure_mapping(code, status):
    bank = FakeBank()
    bank.failure = AnswerBankError(code, "Review the current version.")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(bank)), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/answer-bank",
            json={"action": "approve", "expected_revision": 1},
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == status
        assert result.json()["error"]["code"] == f"answer_bank_{code}"


def test_fixed_cli_adapter(monkeypatch, tmp_path: Path):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(EMPTY), "")

    monkeypatch.setattr(subprocess, "run", run)
    service = AnswerBankService(tmp_path)
    assert service.state().revision == 0
    service.mutate(
        AnswerBankMutation(
            action="approve", expected_revision=0, entry_id="one", expected_version=1
        )
    )
    assert calls[1][0] == [
        str(tmp_path / ".venv/bin/python"),
        "-m",
        "pipeline.answer_bank",
        "--root",
        str(tmp_path),
        "dashboard-mutate",
    ]
    assert "source" not in json.loads(calls[1][1]["input"])
    assert calls[1][1]["cwd"] == tmp_path


@pytest.mark.parametrize("portless", [False, True])
async def test_real_cli_roundtrip_keeps_edited_answer_pending(tmp_path: Path, portless):
    source = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    if not (source / ".venv/bin/python").exists():
        pytest.skip("Sibling pipeline runtime is not installed")
    (tmp_path / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    (tmp_path / "pipeline").symlink_to(source / "pipeline", target_is_directory=True)
    service = AnswerBankService(tmp_path)
    application = app(service)
    headers = {"Origin": ORIGIN}
    if portless:
        application = ProxyHeadersMiddleware(
            application, trusted_hosts=["127.0.0.1", "::1"]
        )
        headers = {
            "Origin": "https://jobfeed.localhost:1355",
            "X-Forwarded-Host": "jobfeed.localhost:1355",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "::1",
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url=ORIGIN
    ) as client:
        create = {
            "action": "create",
            "expected_revision": 0,
            "draft": {
                "question": "Describe your project?",
                "answer": "I built a synthetic test project.",
                "aliases": ["What did you build?"],
                "scope": {"kind": "global"},
                "valid_until": None,
            },
        }
        created = await client.post("/api/answer-bank", json=create, headers=headers)
        assert created.status_code == HTTPStatus.OK, created.text
        bank = created.json()
        entry = bank["entries"][0]
        assert entry["versions"][0]["state"] == "pending_review"
        approved = await client.post(
            "/api/answer-bank",
            json={
                "action": "approve",
                "entry_id": entry["id"],
                "expected_version": 1,
                "expected_revision": bank["revision"],
            },
            headers=headers,
        )
        assert approved.status_code == HTTPStatus.OK, approved.text
        bank = approved.json()
        version = bank["entries"][0]["versions"][0]
        assert version["state"] == "approved"
        assert version["approval"] is not None
        changed = {
            **create,
            "action": "edit",
            "entry_id": entry["id"],
            "expected_version": 1,
            "expected_revision": bank["revision"],
            "draft": {**create["draft"], "answer": "A revised synthetic answer."},
        }
        edited = await client.post("/api/answer-bank", json=changed, headers=headers)
        assert edited.status_code == HTTPStatus.OK, edited.text
        versions = edited.json()["entries"][0]["versions"]
        assert versions[-1]["state"] == "pending_review"
        assert versions[-1]["approval"] is None
        repeated = await client.post("/api/answer-bank", json=changed, headers=headers)
        assert repeated.status_code == HTTPStatus.OK
        assert repeated.json()["revision"] == edited.json()["revision"]
        changed["draft"]["answer"] = "A conflicting stale answer."
        stale = await client.post("/api/answer-bank", json=changed, headers=headers)
        assert stale.status_code == HTTPStatus.CONFLICT


async def test_real_cli_save_approves_the_answer_and_lists_common_questions(
    tmp_path: Path,
):
    source = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    if not (source / ".venv/bin/python").exists():
        pytest.skip("Sibling pipeline runtime is not installed")
    (tmp_path / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    (tmp_path / "pipeline").symlink_to(source / "pipeline", target_is_directory=True)
    headers = {"Origin": ORIGIN}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(AnswerBankService(tmp_path))),
        base_url=ORIGIN,
    ) as client:
        listed = (await client.get("/api/answer-bank")).json()
        gender = next(
            item for item in listed["common_questions"] if item["key"] == "gender"
        )
        assert (gender["group"], gender["entry_id"]) == ("demographic", None)
        assert "Decline to self-identify" in gender["options"]
        save = {
            "action": "save",
            "expected_revision": 0,
            "entry_id": "gender",
            "draft": {
                "question": gender["question"],
                "answer": "Female",
                "scope": {"kind": "global"},
            },
        }
        saved = await client.post("/api/answer-bank", json=save, headers=headers)
        assert saved.status_code == HTTPStatus.OK, saved.text
        body = saved.json()
        assert body["entries"][0]["versions"][0]["state"] == "approved"
        assert (
            next(item for item in body["common_questions"] if item["key"] == "gender")[
                "entry_id"
            ]
            == "gender"
        )
        stale = await client.post(
            "/api/answer-bank",
            json={**save, "draft": {**save["draft"], "answer": "Male"}},
            headers=headers,
        )
        assert stale.status_code == HTTPStatus.CONFLICT


async def test_real_cli_merge_folds_one_answer_into_another(tmp_path: Path):
    source = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    if not (source / ".venv/bin/python").exists():
        pytest.skip("Sibling pipeline runtime is not installed")
    (tmp_path / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    (tmp_path / "pipeline").symlink_to(source / "pipeline", target_is_directory=True)
    headers = {"Origin": ORIGIN}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(AnswerBankService(tmp_path))),
        base_url=ORIGIN,
    ) as client:
        revision = 0
        for entry_id, question in (
            ("kept", "Current employer"),
            ("merged", "Most recent employer"),
        ):
            saved = await client.post(
                "/api/answer-bank",
                json={
                    "action": "save",
                    "expected_revision": revision,
                    "entry_id": entry_id,
                    "draft": {
                        "question": question,
                        "answer": "Example Corp",
                        "scope": {"kind": "global"},
                    },
                },
                headers=headers,
            )
            assert saved.status_code == HTTPStatus.OK, saved.text
            revision = saved.json()["revision"]
        merge = {
            "action": "merge",
            "expected_revision": revision,
            "entry_id": "kept",
            "merge_entry_ids": ["merged"],
        }
        merged = await client.post("/api/answer-bank", json=merge, headers=headers)
        assert merged.status_code == HTTPStatus.OK, merged.text
        entries = {entry["id"]: entry for entry in merged.json()["entries"]}
        assert entries["kept"]["versions"][-1]["aliases"] == ["Most recent employer"]
        assert entries["merged"]["versions"][-1]["state"] == "revoked"
        assert entries["merged"]["versions"][-1]["merged_into"] == "kept"
        replay = await client.post("/api/answer-bank", json=merge, headers=headers)
        assert replay.json()["revision"] == merged.json()["revision"]


async def test_merge_requests_reach_the_answer_bank():
    bank = FakeBank()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(bank)), base_url=ORIGIN
    ) as client:
        merge = {
            "action": "merge",
            "expected_revision": 4,
            "entry_id": "kept",
            "merge_entry_ids": ["one", "two"],
        }
        result = await client.post(
            "/api/answer-bank", json=merge, headers={"Origin": ORIGIN}
        )
        assert result.status_code == HTTPStatus.OK, result.text
        empty = await client.post(
            "/api/answer-bank",
            json={**merge, "merge_entry_ids": []},
            headers={"Origin": ORIGIN},
        )
        assert empty.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert [write.model_dump(exclude_none=True) for write in bank.writes] == [merge]


@pytest.mark.parametrize("host", ["jobfeed.localhost:1355", "127.0.0.1:4654"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_verified_portless_origin_can_save_draft(host, scheme):
    bank = FakeBank()
    proxy = ProxyHeadersMiddleware(app(bank), trusted_hosts=["127.0.0.1", "::1"])
    headers = {
        "Host": host,
        "Origin": f"{scheme}://jobfeed.localhost:1355",
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": scheme,
        "X-Forwarded-For": "::1",
    }
    payload = {
        "action": "edit",
        "entry_id": "one",
        "expected_revision": 0,
        "expected_version": 1,
        "draft": {
            "question": "Why this role?",
            "answer": "My edited synthetic answer.",
            "scope": {"kind": "job", "job_id": "3"},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://127.0.0.1:4654"
    ) as client:
        result = await client.post("/api/answer-bank", json=payload, headers=headers)
    assert result.status_code == HTTPStatus.OK, result.text
    assert result.headers["cache-control"] == "no-store"
    assert len(bank.writes) == 1
    assert bank.writes[0].action == "edit"
    assert bank.writes[0].draft.answer == "My edited synthetic answer."


@pytest.mark.parametrize(
    "header,value",
    [
        ("Origin", "https://attacker.test"),
        ("Origin", "https://jobfeed.localhost:1356"),
        ("Origin", ""),
        ("Host", "attacker.test"),
        ("Host", "127.0.0.1:8765"),
        ("X-Forwarded-Host", "attacker.test"),
        ("X-Forwarded-Proto", "http"),
        ("X-Forwarded-For", "203.0.113.10"),
        ("X-Forwarded-For", "::1, 127.0.0.1"),
    ],
)
async def test_forged_portless_origin_cannot_mutate(header, value):
    await _reject_proxy_request(overrides={header: value})


@pytest.mark.parametrize(
    "header",
    ["Origin", "Host", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-For"],
)
async def test_duplicate_portless_headers_cannot_mutate(header):
    await _reject_proxy_request(duplicate=header)


@pytest.mark.parametrize(
    "peer,backend",
    [
        ("203.0.113.10", "http://127.0.0.1:4654"),
        ("127.0.0.1", "http://203.0.113.10:4654"),
    ],
)
async def test_remote_peer_or_socket_cannot_forge_portless_origin(peer, backend):
    await _reject_proxy_request(peer=peer, backend=backend)


async def _reject_proxy_request(
    *, overrides=None, duplicate=None, peer="127.0.0.1", backend="http://127.0.0.1:4654"
):
    bank = FakeBank()
    proxy = ProxyHeadersMiddleware(app(bank), trusted_hosts=["127.0.0.1", "::1"])
    headers = {
        "Host": "127.0.0.1:4654",
        "Origin": "https://jobfeed.localhost:1355",
        "X-Forwarded-Host": "jobfeed.localhost:1355",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "::1",
        **(overrides or {}),
    }
    values = list(headers.items())
    if duplicate:
        values.append((duplicate, headers[duplicate]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy, client=(peer, 12345)), base_url=backend
    ) as client:
        result = await client.post(
            "/api/answer-bank",
            json={"action": "approve", "expected_revision": 0},
            headers=values,
        )
    assert result.status_code == HTTPStatus.FORBIDDEN
    assert result.json()["error"]["code"] == "answer_bank_origin"
    assert bank.writes == []


@pytest.mark.parametrize(
    "header,value", [("Origin", ORIGIN), ("Host", "127.0.0.1:8765")]
)
async def test_duplicate_direct_headers_cannot_mutate(header, value):
    bank = FakeBank()
    headers = [("Origin", ORIGIN), ("Host", "127.0.0.1:8765"), (header, value)]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(bank)), base_url=ORIGIN
    ) as client:
        result = await client.post(
            "/api/answer-bank",
            json={"action": "approve", "expected_revision": 0},
            headers=headers,
        )
    assert result.status_code == HTTPStatus.FORBIDDEN
    assert bank.writes == []
