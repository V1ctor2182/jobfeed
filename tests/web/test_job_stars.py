"""Star reads and same-origin writes use source IDs and preserve queue authority."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.web.deps import get_store
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.job_stars import router
from tests.support.factories import make_job

OK = 200
NOT_FOUND = 404
FORBIDDEN = 403
INVALID = 422


@pytest.fixture
async def client(tmp_path: Path):
    store = SQLiteStore(tmp_path / "stars.db")
    await store.connect()
    await store.save_job(make_job())
    app = FastAPI()
    install_error_handling(app)
    app.dependency_overrides[get_store] = lambda: store
    app.include_router(router, prefix="/api")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:4654",
        ) as value:
            yield value
    finally:
        await store.close()


async def test_read_and_toggle_annotation(client: httpx.AsyncClient):
    before = await client.get("/api/job-stars?job_ids=1,999,1")
    assert before.status_code == OK
    assert before.json() == {
        "stars": [{"source_job_id": "1", "real_job_id": "1", "starred": False}]
    }
    saved = await client.put(
        "/api/jobs/1/star",
        json={"starred": True},
        headers={"Origin": "http://127.0.0.1:4654"},
    )
    assert saved.status_code == OK
    assert saved.json()["starred"] is True
    assert saved.headers["Cache-Control"] == "no-store"
    assert (await client.get("/api/job-stars?job_ids=1")).json()["stars"] == [
        saved.json()
    ]


@pytest.mark.parametrize(
    "ids", ["", "0", "-1", "abc", "1,", "1," * 100 + "1", str(2**63)]
)
async def test_bounded_ids(client: httpx.AsyncClient, ids: str):
    assert (
        await client.get("/api/job-stars", params={"job_ids": ids})
    ).status_code == INVALID


@pytest.mark.parametrize(
    "origin", [None, "https://example.test", "http://localhost:4654"]
)
async def test_foreign_origin_cannot_toggle(
    client: httpx.AsyncClient, origin: str | None
):
    response = await client.put(
        "/api/jobs/1/star",
        json={"starred": True},
        headers={"Origin": origin} if origin else {},
    )
    assert response.status_code == FORBIDDEN
    assert (await client.get("/api/job-stars?job_ids=1")).json()["stars"][0][
        "starred"
    ] is False


async def test_missing_source_and_strict_boolean(client: httpx.AsyncClient):
    headers = {"Origin": "http://127.0.0.1:4654"}
    assert (
        await client.put("/api/jobs/999/star", json={"starred": True}, headers=headers)
    ).status_code == NOT_FOUND
    assert (
        await client.put("/api/jobs/1/star", json={"starred": "false"}, headers=headers)
    ).status_code == INVALID
