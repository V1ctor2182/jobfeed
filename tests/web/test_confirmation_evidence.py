# ruff: noqa: PLR2004

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest

from jobfeed.apply_health import ApplyHealthReader
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import fake_context, open_client
from tests.web.test_apply_health import (
    NOW,
    make_database,
    make_monitor,
    tree,
    controller_manifest,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAGQAAAAyCAIAAAAlV+npAAAAgklEQVR4nO3SsREAIAwDMWD/ncMK+V6qXf35zsxh5y13iNV4ViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViDW2fto2wNhH84DkgAAAABJRU5ErkJggg=="
)
URL = "https://careers.example.test/jobs/1"


def fixture(tmp_path: Path, *, email: bool = False):
    database = tmp_path / "jobs.sqlite"
    root = tmp_path / "pipeline"
    folder = root / "applications" / "one"
    folder.mkdir(parents=True)
    make_database(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "ALTER TABLE application_queue ADD COLUMN application_folder_relpath TEXT"
        )
        db.execute(
            "UPDATE application_queue SET application_folder_relpath='one' "
            "WHERE job_id=1"
        )
        db.execute("ALTER TABLE jobs ADD COLUMN url TEXT")
        db.execute("UPDATE jobs SET url=? WHERE id=1", (URL,))
    text = "Your application was received."
    capture = {"tool": "browser.screenshot", "source_id": "test-tool-call-1"}
    confirmation = {
        "kind": "website_screenshot",
        "artifact": "confirmation.png",
        "observation": "observation.json",
        "captured_at": NOW.isoformat(),
        "source_url": "https://careers.example.test/confirmation",
        "text": text,
        "capture": capture,
    }
    (folder / "confirmation.png").write_bytes(PNG)
    (folder / "observation.json").write_text(
        json.dumps(
            {
                "job_id": 1,
                "job_url": URL,
                "source_url": confirmation["source_url"],
                "captured_at": confirmation["captured_at"],
                "capture": capture,
                "text": text,
            }
        )
    )
    if email:
        confirmation.update(
            kind="confirmation_email",
            artifact="confirmation.eml",
            capture={
                "tool": "mail.get_message",
                "source_id": "<confirmation-1@example.test>",
            },
        )
        (folder / "confirmation.eml").write_text(
            "From: Careers <careers@example.test>\n"
            "To: Candidate <candidate@example.test>\n"
            "Subject: Application received\n"
            "Message-ID: <confirmation-1@example.test>\n"
            "Date: Wed, 21 Oct 2026 11:00:00 +0000\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "Your application was received.\n" + URL
        )
    (folder / "submission-receipt.json").write_text(
        json.dumps({"job_id": 1, "job_url": URL, "confirmation": confirmation})
    )
    return database, root, folder


@pytest.mark.parametrize("email", [False, True])
async def test_confirmation_is_job_bound_and_read_only(tmp_path, monkeypatch, email):
    database, root, folder = fixture(tmp_path, email=email)
    context = fake_context()
    context["settings"].db.path = database
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(root))
    app = build_web_app(context, static_dir=tmp_path / "no-dist")
    before = tree(tmp_path)
    async with open_client(app) as client:
        response = await client.get("/api/apply-health/jobs/1/confirmation")
        artifact = await client.get("/api/apply-health/jobs/1/confirmation/artifact")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "available"
    assert str(folder) not in response.text
    if email:
        assert data["email"]["subject"] == "Application received"
        assert URL in data["email"]["body"]
        assert artifact.status_code == 404
    else:
        assert artifact.content == PNG
        assert artifact.headers["content-type"] == "image/png"
        assert artifact.headers["cache-control"] == "no-store"
        assert artifact.headers["x-content-type-options"] == "nosniff"
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    "mutation", ["missing", "wrong_job", "wrong_url", "traversal", "symlink"]
)
def test_unsafe_or_missing_evidence_never_becomes_available(tmp_path, mutation):
    database, root, folder = fixture(tmp_path)
    receipt = folder / "submission-receipt.json"
    data = json.loads(receipt.read_text())
    if mutation == "missing":
        (folder / "confirmation.png").unlink()
    elif mutation == "wrong_job":
        data["job_id"] = 2
    elif mutation == "wrong_url":
        data["job_url"] = URL + "different"
    elif mutation == "traversal":
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE application_queue SET application_folder_relpath="
                "'../../outside' WHERE job_id=1"
            )
    else:
        outside = tmp_path / "outside.png"
        outside.write_bytes(PNG)
        (folder / "confirmation.png").unlink()
        (folder / "confirmation.png").symlink_to(outside)
    receipt.write_text(json.dumps(data))
    result = ApplyHealthReader(root).confirmation_response(database, 1)
    assert result.status == "unverified"
    assert result.artifact_url is None


def test_closeout_pointer_recovers_evidence_after_queue_removal(tmp_path):
    database, root, _ = fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM application_queue WHERE job_id=1")
    pointer = root / ".pipeline/submission-closeouts/1.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"job_id": 1, "folder": "one"}))
    assert (
        ApplyHealthReader(root).confirmation_response(database, 1).status == "available"
    )


def test_legacy_success_claim_does_not_count_without_artifact(tmp_path):
    database, root, folder = fixture(tmp_path)
    directory = make_monitor(root)
    controller_manifest(directory)
    path = directory / "monitor.json"
    data = json.loads(path.read_text())
    data["snapshot"]["jobs"][0].update(
        submitted=True, user_submitted=True, synced=True, closed=True
    )
    data["snapshot"]["counts"].update(submitted=1, closed=1)
    data["snapshot"]["complete"] = True
    path.write_text(json.dumps(data))
    (folder / "confirmation.png").unlink()
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    assert not batch.jobs[0].submitted
    assert batch.jobs[0].user_submitted
    assert not batch.jobs[0].synced
    assert batch.counts.submitted == batch.counts.closed == 0
    assert not batch.complete
    assert "submission_unverified" in batch.jobs[0].errors


def test_valid_carryover_confirmation_does_not_count_toward_batch_target(tmp_path):
    database, root, _ = fixture(tmp_path)
    directory = make_monitor(root)
    controller_manifest(directory)
    path = directory / "monitor.json"
    data = json.loads(path.read_text())
    data["snapshot"]["jobs"][0].update(
        submitted=True, synced=True, closed=True, counted=False
    )
    data["snapshot"]["counts"].update(submitted=1, closed=1)
    data["snapshot"]["complete"] = True
    path.write_text(json.dumps(data))
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    assert batch.jobs[0].submitted
    assert not batch.jobs[0].closed
    assert not batch.jobs[0].counted
    assert batch.counts.submitted == batch.counts.closed == 0
    assert not batch.complete
