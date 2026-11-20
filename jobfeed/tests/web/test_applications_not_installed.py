"""A Jobfeed installed without the Applier says so plainly.

Application email is the exception: the tracker runs with Jobfeed's own Python.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobfeed.applications_runtime import NOT_INSTALLED_CODE
from jobfeed.web.app import create_web_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    pipeline = tmp_path / "resume-application-pipeline"
    pipeline.mkdir()
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(pipeline))
    with TestClient(create_web_app()) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/api/review", "/api/answer-bank"])
def test_application_pages_report_that_applying_is_not_set_up(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == HTTPStatus.NOT_FOUND
    error = response.json()["error"]
    assert error["code"] == NOT_INSTALLED_CODE
    assert "isn't set up" in error["message"]


def test_the_tracker_runs_without_the_applier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    real = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    pipeline = tmp_path / "resume-application-pipeline" / "pipeline"
    pipeline.mkdir(parents=True)
    (pipeline / "__init__.py").write_text("")
    shutil.copy(real / "pipeline" / "mail_tracker.py", pipeline / "mail_tracker.py")
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(pipeline.parent))
    with TestClient(create_web_app()) as client:
        response = client.get("/api/tracker")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["last_sync"] is None
