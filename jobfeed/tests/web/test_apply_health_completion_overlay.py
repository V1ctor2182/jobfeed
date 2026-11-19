"""Native completion overlays stale monitors without restarting or rewriting them."""

import json
import os
import sqlite3

import pytest

from jobfeed.apply_health import ApplyHealthReader
from tests.web.test_apply_health import NOW, controller_manifest, make_monitor, tree
from tests.web.test_confirmation_evidence import URL, fixture


def completed(tmp_path, *, applied=True, queue_present=False):
    database, root, folder = fixture(tmp_path)
    directory = make_monitor(root, age=600)
    manifest = controller_manifest(directory)
    manifest.with_name(manifest.stem + "-handoff.json").write_text(
        json.dumps(
            {
                "manifest": str(manifest),
                "controller": "controller-one",
                "status": "stopped_by_user",
            }
        )
    )
    monitor = directory / "monitor.json"
    raw = json.loads(monitor.read_text())
    raw["snapshot"]["jobs"][0].update(
        queue_state="Needs Input",
        paused=True,
        blocked="needs_input",
        next_step="主控处理阻碍",
    )
    raw["snapshot"]["counts"]["paused"] = 1
    monitor.write_text(json.dumps(raw))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE applied(job_id INTEGER)")
        if applied:
            db.execute("INSERT INTO applied VALUES(1)")
        if not queue_present:
            db.execute("DELETE FROM application_queue WHERE job_id=1")
    pointer = root / ".pipeline/submission-closeouts/1.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "job_id": 1,
                "folder": "one",
                "thread_id": "current-owner",
                "complete": True,
            }
        )
    )
    receipt = folder / "submission-receipt.json"
    data = json.loads(receipt.read_text())
    data.update(
        tabs_closed=True,
        closed_tab_ids=["tab-1"],
        tab_close_evidence="tab-close-evidence.json",
    )
    receipt.write_text(json.dumps(data))
    proof = folder / "tab-close-evidence.json"
    proof.write_text(
        json.dumps(
            {
                "job_id": 1,
                "job_url": URL,
                "recorded_at": NOW.isoformat(),
                "capture": {
                    "tool": "browser.close and global inventory",
                    "source_id": "tab-1",
                },
                "closed_tab_ids": ["tab-1"],
                "verified_absent": True,
                "unknown_tab_ids": [],
                "global_inventory_errors": [],
                "same_profile_present": True,
            }
        )
    )
    return database, root, folder, directory, pointer


def test_stale_false_snapshot_upgrades_from_native_completion_read_only(tmp_path):
    database, root, _, directory, _ = completed(tmp_path)
    before = tree(tmp_path)
    raw = json.loads((directory / "monitor.json").read_text())
    result = ApplyHealthReader(root).read(database, now=NOW)
    batch = result.batches[0]
    job = batch.jobs[0]
    assert job.submitted and job.synced and job.closed
    assert job.owner_thread_id == "current-owner"
    assert job.queue_state == "Submitted"
    assert job.blocked is None and not job.paused
    assert job.next_step == "完成，禁止重投"  # noqa: RUF001 - must match the Chinese status text in apply_health
    assert (batch.counts.submitted, batch.counts.synced, batch.counts.closed) == (
        1,
        1,
        1,
    )
    assert (batch.counts.reserved, batch.counts.occupied, batch.counts.paused) == (
        0,
        0,
        0,
    )
    assert batch.complete
    assert batch.freshness == "stale"
    assert batch.checked_at == raw["checked_at"]
    assert batch.controller.worker_status == "stopped_by_user"
    assert tree(tmp_path) == before


def test_native_receipt_without_applied_does_not_claim_sync_or_completion(tmp_path):
    database, root, _, _, _ = completed(tmp_path, applied=False)
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    job = batch.jobs[0]
    assert job.submitted
    assert not job.synced and not job.closed and not batch.complete
    assert job.paused and job.queue_state == "Needs Input"
    assert batch.counts.synced == batch.counts.closed == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_job"])
def test_formal_applied_without_valid_native_receipt_never_upgrades(tmp_path, damage):
    database, root, folder, _, _ = completed(tmp_path)
    if damage == "missing":
        (folder / "confirmation.png").unlink()
    elif damage == "corrupt":
        (folder / "submission-receipt.json").write_text("{")
    else:
        receipt = folder / "submission-receipt.json"
        data = json.loads(receipt.read_text())
        data["job_id"] = 2
        receipt.write_text(json.dumps(data))
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    job = batch.jobs[0]
    assert not job.submitted and not job.synced and not job.closed
    assert not batch.complete
    assert job.queue_state == "Needs Input" and job.paused
    assert "submission_unverified" in job.errors


@pytest.mark.parametrize(
    "damage",
    ["wrong_folder", "wrong_owner", "missing_owner", "bad_inventory", "incomplete"],
)
def test_closeout_requires_exact_folder_owner_and_native_close_proof(tmp_path, damage):
    database, root, folder, _, pointer = completed(tmp_path, queue_present=True)
    close = json.loads(pointer.read_text())
    close["thread_id"] = "owner-one"
    if damage == "wrong_folder":
        close["folder"] = "other"
    elif damage == "wrong_owner":
        close["thread_id"] = "wrong-owner"
    elif damage == "missing_owner":
        close.pop("thread_id")
    elif damage == "incomplete":
        close["complete"] = False
    else:
        proof = folder / "tab-close-evidence.json"
        data = json.loads(proof.read_text())
        data["unknown_tab_ids"] = ["tab-1"]
        proof.write_text(json.dumps(data))
    pointer.write_text(json.dumps(close))
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    job = batch.jobs[0]
    assert job.submitted and job.synced
    assert not job.closed and not batch.complete
    assert job.queue_state == "Needs Input" and job.paused
    assert job.owner_thread_id != "wrong-owner"


def test_claimed_job_outside_current_manifest_cannot_become_complete(tmp_path):
    database, root, _, directory, _ = completed(tmp_path)
    controller_manifest(directory, ordered_candidates=[{"id": 2, "company": "Beta"}])
    monitor = directory / "monitor.json"
    data = json.loads(monitor.read_text())
    data["snapshot"]["jobs"][0].update(submitted=True, synced=True, closed=True)
    monitor.write_text(json.dumps(data))
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    job = batch.jobs[0]
    assert not job.submitted and not job.synced and not job.closed
    assert not batch.complete


def test_jobs_past_one_query_of_ids_are_still_verified(tmp_path, monkeypatch):
    database, root, _, _, _ = completed(tmp_path)
    newer = make_monitor(root, "batch-two")
    controller_manifest(
        newer,
        ordered_candidates=[
            {"id": 2, "company": "Beta"},
            {"id": 3, "company": "Gamma"},
        ],
    )
    monitor = newer / "monitor.json"
    data = json.loads(monitor.read_text())
    first = data["snapshot"]["jobs"][0]
    data["snapshot"]["jobs"] = [dict(first, job_id=2), dict(first, job_id=3)]
    monitor.write_text(json.dumps(data))
    stamp = monitor.stat().st_mtime + 60
    os.utime(monitor, (stamp, stamp))
    monkeypatch.setattr("jobfeed.apply_health._JOB_LIMIT", 2)
    batches = {b.id: b for b in ApplyHealthReader(root).read(database, now=NOW).batches}
    assert [j.job_id for j in batches["batch-two"].jobs] == [2, 3]
    job = batches["batch-one"].jobs[0]
    assert job.submitted and job.synced and job.closed
    assert "submission_unverified" not in job.errors


@pytest.mark.parametrize("column", ["source_job_id", "source_applied_job_id"])
def test_canonical_application_readback_upgrades_without_legacy_applied_row(
    tmp_path, column
):
    database, root, _, _, _ = completed(tmp_path, applied=False)
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE real_job_applications"
            "(source_job_id INTEGER,source_applied_job_id INTEGER)"
        )
        db.execute(f"INSERT INTO real_job_applications({column}) VALUES(1)")
    before = tree(tmp_path)
    batch = ApplyHealthReader(root).read(database, now=NOW).batches[0]
    assert batch.jobs[0].submitted and batch.jobs[0].synced and batch.jobs[0].closed
    assert batch.complete
    assert tree(tmp_path) == before
