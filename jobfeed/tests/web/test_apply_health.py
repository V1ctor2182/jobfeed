"""The dashboard reads saved observations without application side effects."""

# ruff: noqa: PLR2004

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobfeed.apply_health import ApplyHealthReader, _queue_rows, read_queue
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import fake_context, open_client

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
SECRET = "private-authorization-secret"


def make_database(path: Path, *, metadata: bool = True) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE application_queue(job_id INTEGER, state TEXT, "
            "retry_count INTEGER, "
            "next_retry_at TEXT, codex_application_session_id TEXT, "
            "submit_authorization TEXT)"
        )
        db.executemany(
            "INSERT INTO application_queue VALUES(?,?,0,NULL,?,?)",
            [
                (1, "Applying", "owner-one", SECRET),
                (2, "Needs Input", None, SECRET),
                (3, "Queued", None, SECRET),
            ],
        )
        if metadata:
            db.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT)"
            )
            db.executemany(
                "INSERT INTO jobs VALUES(?,?,?)",
                [
                    (1, "Acme", "Engineer"),
                    (2, "Beta", "Designer"),
                    (3, "Gamma", "Analyst"),
                ],
            )


def make_monitor(root: Path, name: str = "batch-one", *, age: int = 0) -> Path:
    checked = NOW - timedelta(seconds=age)
    manifest = root / ".pipeline" / "batches" / f"{name}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"target_successful_submissions": 1}')
    os.utime(manifest, (checked.timestamp() - 1, checked.timestamp() - 1))
    directory = root / "monitor-output" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "monitor.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": str(manifest),
                "checked_at": checked.isoformat(),
                "cursors": {"secret": SECRET},
                "snapshot": {
                    "checked_at": checked.isoformat(),
                    "target": 1,
                    "complete": False,
                    "counts": {
                        "submitted": 0,
                        "synced": 0,
                        "closed": 0,
                        "reserved": 1,
                        "occupied": 1,
                        "paused": 0,
                    },
                    "blockers": {
                        "queue": [{"secret": SECRET}],
                        "parked": [],
                        "waiting": [],
                    },
                    "jobs": [
                        {
                            "job_id": 1,
                            "company": "Acme",
                            "title": "Engineer",
                            "queue_state": "Applying",
                            "turn_state": "active",
                            "owner": "owner-one",
                            "submitted": False,
                            "synced": False,
                            "closed": False,
                            "paused": False,
                            "errors": [],
                            "usage": {
                                "requests": 2,
                                "total_tokens": 480,
                                "secret": SECRET,
                            },
                            "result_quote": SECRET,
                            "profile": SECRET,
                        }
                    ],
                    "profile": SECRET,
                },
            }
        )
    )
    os.utime(directory / "monitor.json", (checked.timestamp(), checked.timestamp()))
    return directory


def mutate(directory: Path, **values: object) -> None:
    path = directory / "monitor.json"
    raw = json.loads(path.read_text())
    raw.update(values)
    path.write_text(json.dumps(raw))


def tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def controller_manifest(directory: Path, **fields: object) -> Path:
    source = json.loads((directory / "monitor.json").read_text())["source"]
    manifest = Path(source)
    manifest.write_text(
        json.dumps(
            {
                "controller_thread_id": "controller-one",
                "max_parallel": 1,
                "ordered_candidates": [
                    {"id": 2, "company": "Beta", "private": SECRET},
                    {"id": 1, "company": "Acme", "private": SECRET},
                ],
                "private": SECRET,
                **fields,
            }
        )
    )
    return manifest


def test_controller_and_applier_metadata_are_bound_and_read_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "apply"
    directory = make_monitor(root, age=600)
    manifest = controller_manifest(directory)
    manifest.with_name(manifest.stem + "-handoff.json").write_text(
        json.dumps(
            {
                "manifest": str(manifest),
                "controller": "controller-one",
                "status": "stopped_by_user",
                "command": SECRET,
            }
        )
    )
    before = tree(tmp_path)

    result = ApplyHealthReader(root).read(tmp_path / "missing.sqlite", now=NOW)

    batch = result.batches[0]
    assert batch.freshness == "stale"
    assert batch.controller is not None
    assert batch.controller.model_dump() == {
        "thread_id": "controller-one",
        "display_name": "Beta, Acme applications",
        "dispatch_mode": "controller",
        "dispatcher_thread_id": None,
        "max_parallel": 1,
        "worker_status": "stopped_by_user",
    }
    assert batch.jobs[0].applier_label == "B"
    assert SECRET not in result.model_dump_json()
    assert tree(tmp_path) == before


@pytest.mark.parametrize("capacity", [None, 0, -1, 201, True, "4"])
def test_invalid_capacity_is_not_presented_as_scheduler_slots(
    tmp_path: Path, capacity: object
) -> None:
    directory = make_monitor(tmp_path / "apply")
    controller_manifest(directory, max_parallel=capacity)
    result = ApplyHealthReader(tmp_path / "apply").read(tmp_path / "none", now=NOW)
    assert result.batches[0].controller is not None
    assert result.batches[0].controller.max_parallel is None


def test_controller_name_and_separate_dispatcher_are_allowlisted(
    tmp_path: Path,
) -> None:
    directory = make_monitor(tmp_path / "apply")
    controller_manifest(
        directory,
        controller_display_name="Engineering applications",
        desktop_dispatcher_thread_id="dispatcher-one",
    )
    result = ApplyHealthReader(tmp_path / "apply").read(tmp_path / "none", now=NOW)
    controller = result.batches[0].controller
    assert controller is not None
    assert controller.display_name == "Engineering applications"
    assert controller.dispatch_mode == "separate_dispatcher"
    assert controller.dispatcher_thread_id == "dispatcher-one"


@pytest.mark.parametrize("registered", [None, "different-controller"])
def test_unknown_or_mismatched_registered_controller_is_not_projected(
    tmp_path: Path, registered: str | None
) -> None:
    root = tmp_path / "apply"
    directory = make_monitor(root)
    manifest = controller_manifest(directory)
    with sqlite3.connect(root / ".pipeline" / "blockers.sqlite3") as db:
        db.execute("CREATE TABLE dq_scopes(scope TEXT, controller TEXT)")
        if registered:
            db.execute(
                "INSERT INTO dq_scopes VALUES (?, ?)", (str(manifest), registered)
            )
    before = tree(tmp_path)
    result = ApplyHealthReader(root).read(tmp_path / "none", now=NOW)
    assert result.batches[0].controller is None
    assert "controller_registration_mismatch" in result.batches[0].issues
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"controller_thread_id": "bad\ncontroller"},
        {"ordered_candidates": [{"id": 1}, {"id": 1}]},
        {"ordered_candidates": [{"id": True}]},
        {"ordered_candidates": [{"id": i + 1} for i in range(201)]},
    ],
)
def test_invalid_manifest_metadata_is_not_a_controller(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    directory = make_monitor(tmp_path / "apply")
    controller_manifest(directory, **fields)
    result = ApplyHealthReader(tmp_path / "apply").read(tmp_path / "none", now=NOW)
    assert result.batches[0].controller is None
    assert result.batches[0].jobs[0].job_id == 1


@pytest.mark.parametrize("handoff_state", ["unknown", "mismatch", "escape"])
def test_unbound_or_unknown_handoff_does_not_expose_worker_status(
    tmp_path: Path, handoff_state: str
) -> None:
    root = tmp_path / "apply"
    directory = make_monitor(root)
    manifest = controller_manifest(directory)
    handoff = manifest.with_name(manifest.stem + "-handoff.json")
    value = {
        "manifest": str(manifest) if handoff_state != "mismatch" else "elsewhere",
        "status": SECRET if handoff_state == "unknown" else "stopped_by_user",
    }
    if handoff_state == "escape":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(value))
        handoff.symlink_to(outside)
    else:
        handoff.write_text(json.dumps(value))
    result = ApplyHealthReader(root).read(tmp_path / "none", now=NOW)
    assert result.batches[0].controller is not None
    assert result.batches[0].controller.worker_status is None
    assert SECRET not in result.model_dump_json()


def test_applier_labels_follow_manifest_order_beyond_z(tmp_path: Path) -> None:
    directory = make_monitor(tmp_path / "apply")
    controller_manifest(
        directory, ordered_candidates=[{"id": i} for i in range(2, 28)] + [{"id": 1}]
    )
    raw = json.loads((directory / "monitor.json").read_text())
    raw["snapshot"]["jobs"][0]["applier_label"] = SECRET
    mutate(directory, snapshot=raw["snapshot"])
    result = ApplyHealthReader(tmp_path / "apply").read(tmp_path / "none", now=NOW)
    assert result.batches[0].jobs[0].applier_label == "AA"
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://careers.example.com/jobs/1?source=board",
            "https://careers.example.com/jobs/1?source=board",
        ),
        ("http://jobs.example.com/1", "http://jobs.example.com/1"),
        (None, None),
        ("", None),
        ("javascript:alert(1)", None),
        ("file:///private/application.html", None),
        ("https:///jobs/1", None),
        ("https://user:password@example.com/job", None),
        ("https://example.com\\@other.example/job", None),
        ("https://example.com/\njob", None),
        ("https://example.com/\x00job", None),
        ("https://example.com/\x7fjob", None),
        ("https://example.com:invalid/job", None),
        ("https://[invalid/job", None),
        ("https://example.com/" + "a" * 4096, None),
    ],
)
def test_observed_job_url_comes_only_from_database(
    tmp_path: Path, url: str | None, expected: str | None
) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE jobs ADD COLUMN url TEXT")
        db.execute("UPDATE jobs SET url=? WHERE id=1", (url,))
    directory = make_monitor(tmp_path / "apply")
    raw = json.loads((directory / "monitor.json").read_text())
    raw["snapshot"]["jobs"][0]["job_url"] = "https://forged.example/private-form"
    mutate(directory, snapshot=raw["snapshot"])
    before = tree(tmp_path)

    result = ApplyHealthReader(tmp_path / "apply").read(database, now=NOW)

    job = result.batches[0].jobs[0]
    assert job.job_url == expected
    assert job.title == "Engineer"
    assert job.queue_state == "Applying"
    assert "forged.example" not in result.model_dump_json()
    assert tree(tmp_path) == before


@pytest.mark.parametrize("database_state", ["missing", "no_url_column", "no_job"])
def test_missing_posting_metadata_preserves_observed_jobs(
    tmp_path: Path, database_state: str
) -> None:
    database = tmp_path / "jobs.sqlite"
    if database_state != "missing":
        make_database(database)
        if database_state == "no_job":
            with sqlite3.connect(database) as db:
                db.execute("ALTER TABLE jobs ADD COLUMN url TEXT")
                db.execute("DELETE FROM jobs WHERE id=1")
    make_monitor(tmp_path / "apply")
    before = tree(tmp_path)

    result = ApplyHealthReader(tmp_path / "apply").read(database, now=NOW)

    assert result.monitoring.state == "available"
    assert result.batches[0].jobs[0].job_url is None
    assert result.batches[0].jobs[0].queue_state == "Applying"
    assert tree(tmp_path) == before


def test_observed_posting_url_is_not_limited_to_queue_subset(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE jobs ADD COLUMN url TEXT")
        db.executemany(
            "INSERT INTO application_queue VALUES(?, 'Queued', 0, NULL, NULL, NULL)",
            [(job_id,) for job_id in range(4, 204)],
        )
        db.execute(
            "INSERT INTO jobs VALUES(203, 'Acme', 'Engineer', 'https://careers.example.com/jobs/203')"
        )
    directory = make_monitor(tmp_path / "apply")
    raw = json.loads((directory / "monitor.json").read_text())
    raw["snapshot"]["jobs"][0]["job_id"] = 203
    mutate(directory, snapshot=raw["snapshot"])

    result = ApplyHealthReader(tmp_path / "apply").read(database, now=NOW)

    assert result.queue.truncated
    assert 203 not in {job.job_id for job in result.queue.jobs}
    assert result.batches[0].jobs[0].job_url == "https://careers.example.com/jobs/203"


@pytest.mark.parametrize("source_state", ["invalid", "missing"])
def test_invalid_observation_source_does_not_expose_posting_url(
    tmp_path: Path, source_state: str
) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE jobs ADD COLUMN url TEXT")
        db.execute(
            "UPDATE jobs SET url='https://careers.example.com/jobs/1' WHERE id=1"
        )
    directory = make_monitor(tmp_path / "apply")
    raw = json.loads((directory / "monitor.json").read_text())
    raw["snapshot"]["jobs"][0]["job_url"] = "https://forged.example/private-form"
    mutate(
        directory,
        source=None if source_state == "missing" else str(tmp_path / "outside.json"),
        snapshot=raw["snapshot"],
    )

    result = ApplyHealthReader(tmp_path / "apply").read(database, now=NOW)

    assert result.batches[0].source_binding == source_state
    assert "careers.example.com" not in result.model_dump_json()
    assert "forged.example" not in result.model_dump_json()


def test_queue_and_saved_batches_are_separate_allowlisted_observations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    root = tmp_path / "apply"
    first = make_monitor(root)
    make_monitor(root, "batch-two", age=600)
    source = json.loads((first / "monitor.json").read_text())["source"]
    (first / "queue-health.json").write_text(
        json.dumps(
            {
                "version": 1,
                "scope": source,
                "controller": "controller-one",
                "recovered": [],
                "active": {
                    "scope": source,
                    "status": "unknown",
                    "error_type": "OperationalError",
                    "first_seen": NOW.isoformat(),
                    "last_seen": NOW.isoformat(),
                    "attempt": {"text": SECRET},
                    "receipt": SECRET,
                },
            }
        )
    )
    before = tree(tmp_path)
    result = ApplyHealthReader(root).read(database, now=NOW)
    assert result.runtime.state == "available"
    assert result.queue.total == 3
    assert result.queue.counts_by_state == {
        "Applying": 1,
        "Needs Input": 1,
        "Queued": 1,
    }
    assert result.queue.jobs[2].company == "Gamma"
    assert [batch.freshness for batch in result.batches] == ["fresh", "stale"]
    assert [batch.jobs[0].job_id for batch in result.batches] == [1, 1]
    assert result.batches[0].blockers is not None
    assert result.batches[0].blockers.pending_controller == 1
    assert result.batches[0].queue_health.state == "active_issue"
    assert result.batches[0].queue_health.delivery_state == "unknown"
    assert result.batches[0].queue_health.error_type == "OperationalError"
    assert SECRET not in result.model_dump_json()
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    ("age", "freshness"),
    [(300, "fresh"), (301, "stale"), (-31, "invalid"), (-30, "fresh")],
)
def test_freshness_boundaries(tmp_path: Path, age: int, freshness: str) -> None:
    make_monitor(tmp_path, age=age)
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.batches[0].freshness == freshness


@pytest.mark.parametrize("checked", ["2026-10-21T12:00:00", "invalid", None, 123])
def test_invalid_timestamps_are_not_recent(tmp_path: Path, checked: object) -> None:
    directory = make_monitor(tmp_path)
    mutate(directory, checked_at=checked)
    batch = (
        ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    )
    assert batch.freshness == "invalid"
    assert batch.issues == ["invalid_timestamp"]


def test_manifest_changes_are_reported_as_stale(tmp_path: Path) -> None:
    directory = make_monitor(tmp_path)
    source = Path(json.loads((directory / "monitor.json").read_text())["source"])
    os.utime(source, (NOW.timestamp() + 1, NOW.timestamp() + 1))
    batch = (
        ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    )
    assert batch.freshness == "stale"
    assert batch.issues == ["manifest_changed_since_observation"]


@pytest.mark.parametrize(
    "source", ["../other/batch.json", "/private/tmp/batch.json", None]
)
def test_source_must_bind_to_existing_local_manifest(
    tmp_path: Path, source: object
) -> None:
    directory = make_monitor(tmp_path)
    mutate(directory, source=source)
    batch = (
        ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    )
    assert batch.source_binding in {"invalid", "missing"}
    assert batch.freshness != "fresh"
    assert batch.jobs == []


@pytest.mark.parametrize("content", ["not JSON", "[]", "null", "{}"])
def test_invalid_files_are_not_empty_healthy_batches(
    tmp_path: Path, content: str
) -> None:
    directory = make_monitor(tmp_path)
    (directory / "monitor.json").write_text(content)
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.monitoring.discovered_batches == 1
    assert result.batches[0].freshness == "invalid"
    assert result.batches[0].issues


def test_missing_runtime_does_not_create_any_files(tmp_path: Path) -> None:
    result = ApplyHealthReader(tmp_path / "missing").read(
        tmp_path / "missing.sqlite", now=NOW
    )
    assert result.runtime.state == "missing"
    assert result.queue.state == "missing"
    assert result.queue.total is None
    assert result.monitoring.state == "missing"
    assert list(tmp_path.iterdir()) == []


def test_relative_override_is_unavailable(tmp_path: Path) -> None:
    result = ApplyHealthReader(Path("relative"), "explicit_override").read(
        tmp_path / "missing.sqlite", now=NOW
    )
    assert result.runtime.state == "unavailable"
    assert result.runtime.error == "apply_root_must_be_absolute"


def test_queue_absence_corruption_and_metadata_failure(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(path):
        pass
    reader = ApplyHealthReader(tmp_path)
    missing = reader.read(path, now=NOW).queue
    assert missing.state == "missing"
    assert missing.total is None
    make_database(path, metadata=False)
    metadata = reader.read(path, now=NOW).queue
    assert metadata.state == "available"
    assert metadata.total == 3
    assert metadata.error == "job_metadata_unavailable"
    assert metadata.jobs[0].company is None
    path.write_bytes(b"corrupted database")
    unavailable = reader.read(path, now=NOW).queue
    assert unavailable.state == "unavailable"
    assert unavailable.total is None
    assert unavailable.error == "database_unreadable"


def test_file_and_directory_symlink_escape_are_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "apply"
    directory = make_monitor(root)
    outside = tmp_path / "outside.json"
    outside.write_text((directory / "monitor.json").read_text())
    (directory / "monitor.json").unlink()
    (directory / "monitor.json").symlink_to(outside)
    result = ApplyHealthReader(root).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.batches[0].freshness == "unavailable"
    assert result.batches[0].issues == ["path_outside_runtime"]
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "monitor-output").symlink_to(root / "monitor-output")
    assert (
        ApplyHealthReader(other_root)
        .read(tmp_path / "absent.sqlite", now=NOW)
        .monitoring.state
        == "unavailable"
    )


def test_oversize_snapshot_is_bounded(tmp_path: Path) -> None:
    directory = make_monitor(tmp_path)
    (directory / "monitor.json").write_bytes(b" " * (4 * 1024 * 1024 + 1))
    batch = (
        ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    )
    assert batch.freshness == "unavailable"
    assert batch.issues == ["file_too_large"]


def test_missing_and_inactive_queue_health_are_distinct(tmp_path: Path) -> None:
    directory = make_monitor(tmp_path)
    reader = ApplyHealthReader(tmp_path)
    missing = reader.read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    assert missing.queue_health.state == "missing"
    source = json.loads((directory / "monitor.json").read_text())["source"]
    path = directory / "queue-health.json"
    path.write_text(
        json.dumps({"version": 1, "scope": source, "active": None, "recovered": []})
    )
    recorded = reader.read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    assert recorded.queue_health.state == "no_active_issue_recorded"
    path.write_text(
        json.dumps(
            {"version": 1, "scope": "another-batch", "active": None, "recovered": []}
        )
    )
    invalid = reader.read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    assert invalid.queue_health.state == "invalid"


async def test_production_factory_route_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "pipeline"
    make_monitor(runtime)
    db_path = tmp_path / "jobs.sqlite"
    make_database(db_path)
    context = fake_context()
    context["settings"].db.path = db_path
    context["config_path"] = tmp_path / "configuration" / "config.toml"
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(runtime))
    app = build_web_app(context, static_dir=tmp_path / "no-dist")
    before = tree(tmp_path)
    async with open_client(app) as client:
        response = await client.get("/api/apply-health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"] == {
        "state": "available",
        "source": "explicit_override",
        "error": None,
    }
    assert payload["queue"]["total"] == 3
    assert payload["mode"] == "read_only"
    assert SECRET not in response.text
    assert tree(tmp_path) == before


def test_default_runtime_is_repository_sibling_not_config_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOBFEED_APPLY_ROOT", raising=False)
    context = fake_context()
    context["config_path"] = tmp_path / "configuration" / "config.toml"
    app = build_web_app(context, static_dir=tmp_path / "no-dist")
    assert (
        app.state.apply_health_reader.root
        == Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    )


def test_real_monitor_diagnostics_are_projected_without_raw_evidence(
    tmp_path: Path,
) -> None:
    directory = make_monitor(tmp_path)
    path = directory / "monitor.json"
    raw = json.loads(path.read_text())
    raw["snapshot"]["errors"] = ["jobfeed_readback:OperationalError", SECRET]
    raw["snapshot"]["gap"] = SECRET
    raw["snapshot"]["coordination_error"] = "OperationalError"
    raw["snapshot"]["jobs"][0]["errors"] = [
        "submission-receipt.json:invalid",
        "owner_conflict",
        "/private/" + SECRET,
    ]
    path.write_text(json.dumps(raw))
    batch = (
        ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW).batches[0]
    )
    assert batch.freshness == "fresh"
    assert batch.issues == [
        "monitor_errors_recorded",
        "coordination_error_recorded",
        "monitor_gap_recorded",
    ]
    assert batch.jobs[0].errors == [
        "invalid_artifact",
        "owner_conflict",
        "unrecognized_monitor_error",
    ]
    assert SECRET not in batch.model_dump_json()


def test_result_status_is_projected_only_as_a_code(tmp_path: Path) -> None:
    directory = make_monitor(tmp_path)
    path = directory / "monitor.json"
    raw = json.loads(path.read_text())
    raw["snapshot"]["jobs"][0]["result_status"] = "ready_for_review"
    path.write_text(json.dumps(raw))
    reader = ApplyHealthReader(tmp_path)
    job = reader.read(tmp_path / "absent.sqlite", now=NOW).batches[0].jobs[0]
    assert job.result_status == "ready_for_review"
    raw["snapshot"]["jobs"][0]["result_status"] = "/private/" + SECRET
    path.write_text(json.dumps(raw))
    job = reader.read(tmp_path / "absent.sqlite", now=NOW).batches[0].jobs[0]
    assert job.result_status is None


def test_queue_and_batch_rows_are_capped_without_losing_counts(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    with sqlite3.connect(database) as db:
        db.executemany(
            "INSERT INTO application_queue VALUES(?, 'Queued', 0, NULL, NULL, NULL)",
            [(n,) for n in range(4, 205)],
        )
    directory = make_monitor(tmp_path)
    path = directory / "monitor.json"
    raw = json.loads(path.read_text())
    raw["snapshot"]["jobs"] *= 210
    path.write_text(json.dumps(raw))
    result = ApplyHealthReader(tmp_path).read(database, now=NOW)
    assert result.queue.total == 204
    assert result.queue.counts_by_state["Queued"] == 202
    assert result.queue.truncated is True
    assert len(result.queue.jobs) == 200
    assert result.batches[0].jobs_truncated is True
    assert len(result.batches[0].jobs) == 200


def test_batch_discovery_is_bounded(tmp_path: Path) -> None:
    for index in range(52):
        (tmp_path / "monitor-output" / f"batch-{index:03}").mkdir(parents=True)
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.monitoring.truncated is True
    assert result.monitoring.discovered_batches == 50
    assert len(result.batches) == 50
    assert all(batch.freshness == "missing" for batch in result.batches)


def test_total_read_budget_is_bounded(tmp_path: Path) -> None:
    for index in range(6):
        directory = make_monitor(tmp_path, f"batch-{index}")
        path = directory / "monitor.json"
        raw = json.loads(path.read_text())
        raw["unused"] = "x" * (3 * 1024 * 1024)
        path.write_text(json.dumps(raw))
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.batches[0].freshness == "fresh"
    assert result.batches[-1].freshness == "unavailable"
    assert result.batches[-1].issues in [["file_too_large"], ["read_budget_exceeded"]]


@pytest.mark.parametrize("value", [[], None, {"counts": []}, {"jobs": "secret"}])
def test_invalid_snapshot_shapes_are_honest(tmp_path: Path, value: object) -> None:
    directory = make_monitor(tmp_path)
    mutate(directory, snapshot=value)
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.batches[0].freshness == "invalid"
    assert result.batches[0].issues


def test_newest_observation_after_first_fifty_directories_is_included(
    tmp_path: Path,
) -> None:
    for index in range(51):
        make_monitor(tmp_path, f"historical-{index:03}", age=600)
    make_monitor(tmp_path, "current-batch")
    result = ApplyHealthReader(tmp_path).read(tmp_path / "absent.sqlite", now=NOW)
    assert result.monitoring.truncated is True
    assert len(result.batches) == 50
    assert result.batches[0].id == "current-batch"
    assert result.batches[0].freshness == "fresh"


def test_manual_priority_is_visible_and_sorted_without_changing_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.sqlite"
    make_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "ALTER TABLE application_queue ADD COLUMN manual_priority "
            "INTEGER NOT NULL DEFAULT 0"
        )
        db.execute("UPDATE application_queue SET manual_priority=1 WHERE job_id=3")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        jobs, error = _queue_rows(db)
    assert error is None
    assert [job.job_id for job in jobs] == [3, 1, 2]
    assert [job.manual_priority for job in jobs] == [True, False, False]
    assert [job.queue_state for job in jobs] == ["Queued", "Applying", "Needs Input"]


def test_an_active_captcha_hold_is_visible_and_a_cleared_one_is_not(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.sqlite"
    make_database(path)
    held = {
        "id": "report-3",
        "kind": "captcha",
        "detail": "Cloudflare asks to verify you are human.",
        "page_url": "https://jobs.example.test/apply/3",
        "created_at": "2026-11-04T09:30:00+00:00",
        "cleared_at": None,
        "cleared_by": None,
    }
    cleared = held | {
        "id": "report-1",
        "cleared_at": "2026-11-04T10:00:00+00:00",
        "cleared_by": "discord:400",
    }
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE application_queue ADD COLUMN hold TEXT")
        db.execute(
            "UPDATE application_queue SET hold=? WHERE job_id=3", (json.dumps(held),)
        )
        db.execute(
            "UPDATE application_queue SET hold=? WHERE job_id=1",
            (json.dumps(cleared),),
        )
    jobs = {job.job_id: job for job in read_queue(path).jobs}
    assert jobs[3].hold is not None
    assert (jobs[3].hold.kind, jobs[3].hold.detail, jobs[3].hold.page_url) == (
        "captcha",
        "Cloudflare asks to verify you are human.",
        "https://jobs.example.test/apply/3",
    )
    assert jobs[3].queue_state == "Queued"
    assert jobs[1].hold is None
    assert jobs[2].hold is None
