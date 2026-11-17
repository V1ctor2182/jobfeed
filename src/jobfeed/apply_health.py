"""Bounded, read-only projection of persisted application observations."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from jobfeed.services.apply_health_models import (
    ApplyHealthResponse,
    BatchHealth,
    BlockerCounts,
    ConfirmationResponse,
    ControllerHealth,
    MonitoringHealth,
    ObservationCounts,
    ObservedJob,
    QueueHealth,
    QueueHold,
    QueueIssueHealth,
    QueueJob,
    RuntimeHealth,
)
from jobfeed.submission_evidence import ConfirmationEvidence, read_confirmation

_FILE_LIMIT = 4 * 1024 * 1024
_TOTAL_LIMIT = 16 * 1024 * 1024
_BATCH_LIMIT = 50
_DIRECTORY_LIMIT = 500
_JOB_LIMIT = 200
_COMPANY_SUMMARY_LIMIT = 3
_URL_LIMIT = 4096
_UNSAFE_URL_CHARACTERS = re.compile(r"[\s\x00-\x1f\x7f\\]")
_FRESH_SECONDS = 300
_FUTURE_SKEW_SECONDS = 30
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}\Z")
_STATE = re.compile(r"[A-Za-z][A-Za-z0-9_ -]{0,99}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_NEXT_STEPS = frozenset(
    {
        "岗位已终止且关页已核验\uff0c禁止重投",
        "岗位已终止\uff0c禁止重投\uff1b原 owner 核验专属页关闭",
        "完成\uff0c禁止重投",
        "用户确认已提交\uff1a补正式同步\uff0c禁止重投",
        "补正式同步\uff0c禁止重投",
        "用户确认已提交\uff1a原 owner 仅关页收尾\uff0c禁止重投",
        "原 owner 关页收尾",
        "保留页面\uff0c不恢复",
        "核查提交结果\uff0c禁止重投",
        "主控处理阻碍",
        "等待原任务证据",
        "等待现有 worker 调度",
    }
)


class EvidenceError(Exception):
    """A bounded evidence read failed, with a public state and safe reason code."""

    def __init__(self, code: str, state: str = "invalid") -> None:
        super().__init__(code)
        self.code = code
        self.state = state


@dataclass
class ReadBudget:
    """Limit JSON evidence reads to one runtime root and a shared byte budget."""

    root: Path
    remaining: int = _TOTAL_LIMIT

    def object(self, path: Path) -> dict[str, object]:
        """Read one root-confined JSON object within the remaining byte budget.

        Args:
            path: Evidence file inside the configured runtime root.

        Returns:
            The decoded object with string keys.

        Raises:
            EvidenceError: The file is unavailable, oversized, or invalid.
        """
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.root):
                raise EvidenceError("path_outside_runtime", "unavailable")
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(resolved, flags), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise EvidenceError("not_regular_file", "unavailable")
                if self.remaining <= 0:
                    raise EvidenceError("read_budget_exceeded", "unavailable")
                limit = min(_FILE_LIMIT, self.remaining)
                raw = stream.read(limit + 1)
            self.remaining -= len(raw)
            if len(raw) > limit:
                raise EvidenceError("file_too_large", "unavailable")
            value = json.loads(raw)
        except FileNotFoundError as exc:
            raise EvidenceError("file_missing", "missing") from exc
        except (OSError, RuntimeError) as exc:
            raise EvidenceError("file_unreadable", "unavailable") from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise EvidenceError("invalid_json") from exc
        return _object(value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise EvidenceError("invalid_object")
    return cast(dict[str, object], value)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise EvidenceError("invalid_timestamp") from exc


def _optional_timestamp(value: object) -> str | None:
    return None if value is None else _timestamp(value).isoformat()


def _code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise EvidenceError("invalid_code")
    return value


def _queue_state(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _STATE.fullmatch(value):
        raise EvidenceError("invalid_queue_state")
    return value


def _identifier(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceError("invalid_identifier")
    return value


def _monitor_error(value: object) -> str:
    if not isinstance(value, str):
        raise EvidenceError("invalid_job_errors")
    if _CODE.fullmatch(value):
        return value
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}:invalid", value):
        return "invalid_artifact"
    return "unrecognized_monitor_error"


def _snapshot_issues(snapshot: dict[str, object]) -> list[str]:
    issues = []
    for key in ("errors", "dispatch_errors"):
        saved_errors = snapshot.get(key, [])
        if not isinstance(saved_errors, list):
            raise EvidenceError("invalid_snapshot_errors")
        if saved_errors:
            issues.append(f"monitor_{key}_recorded")
    if snapshot.get("coordination_error"):
        issues.append("coordination_error_recorded")
    if snapshot.get("gap"):
        issues.append("monitor_gap_recorded")
    return issues


def _observed_job(value: object) -> ObservedJob:
    raw = _object(value)
    projected = {key: raw[key] for key in ObservedJob.model_fields if key in raw}
    projected["job_url"] = None
    projected["applier_label"] = None
    projected["owner_thread_id"] = _identifier(raw.get("owner"))
    projected["queue_state"] = _queue_state(raw.get("queue_state"))
    for name in ("turn_state", "blocked"):
        projected[name] = _code(raw.get(name))
    # An agent's free-form result must not invalidate the whole observation.
    result = raw.get("result_status")
    projected["result_status"] = (
        result if isinstance(result, str) and _CODE.fullmatch(result) else None
    )
    for name in ("last_tool_at", "turn_at"):
        projected[name] = _optional_timestamp(raw.get(name))
    errors = raw.get("errors", [])
    if not isinstance(errors, list):
        raise EvidenceError("invalid_job_errors")
    projected["errors"] = [_monitor_error(error) for error in errors[:50]]

    projected["next_step"] = (
        raw.get("next_step") if raw.get("next_step") in _NEXT_STEPS else None
    )
    return ObservedJob.model_validate(projected)


def _posting_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _URL_LIMIT
        or _UNSAFE_URL_CHARACTERS.search(value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return value


def _populate_job_urls(db_path: Path, batches: list[BatchHealth]) -> None:
    jobs = [
        job
        for batch in batches[:_BATCH_LIMIT]
        if batch.source_binding == "valid"
        for job in batch.jobs[:_JOB_LIMIT]
    ]
    identifiers = list(dict.fromkeys(job.job_id for job in jobs))
    if not identifiers:
        return
    urls: dict[int, str | None] = {}
    try:
        with closing(
            sqlite3.connect(
                db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            for offset in range(0, len(identifiers), _JOB_LIMIT):
                selected = identifiers[offset : offset + _JOB_LIMIT]
                placeholders = ",".join("?" for _ in selected)
                rows = db.execute(
                    "SELECT id, CASE WHEN length(url) <= ? THEN url END "
                    f"FROM jobs WHERE id IN ({placeholders})",
                    [_URL_LIMIT, *selected],
                )
                urls.update((job_id, _posting_url(url)) for job_id, url in rows)
    except (sqlite3.Error, OSError, ValueError):
        return
    for job in jobs:
        job.job_url = urls.get(job.job_id)


def _blockers(value: object) -> BlockerCounts | None:
    if value is None:
        return None
    raw = _object(value)
    groups = [raw.get(key) for key in ("queue", "parked", "waiting")]
    if not all(isinstance(group, list) for group in groups):
        raise EvidenceError("invalid_blockers")
    sizes = [len(cast(list[object], group)) for group in groups]
    return BlockerCounts(
        pending_controller=sizes[0], awaiting_user=sizes[1], waiting_result=sizes[2]
    )


def _queue_issue(
    budget: ReadBudget, directory: Path, source: object
) -> QueueIssueHealth:
    try:
        raw = budget.object(directory / "queue-health.json")
        if raw.get("version") != 1 or "active" not in raw:
            raise EvidenceError("invalid_queue_health")
        if not isinstance(source, str) or raw.get("scope") != source:
            raise EvidenceError("queue_health_scope_mismatch")
        if not isinstance(raw.get("recovered"), list):
            raise EvidenceError("invalid_queue_health")
        if raw["active"] is None:
            return QueueIssueHealth(state="no_active_issue_recorded")
        active = _object(raw["active"])
        if active.get("scope") != source:
            raise EvidenceError("queue_health_scope_mismatch")
        delivery = active.get("status")
        if delivery not in {"pending", "unknown", "delivered", "cancelled"}:
            raise EvidenceError("invalid_queue_health")
        return QueueIssueHealth(
            state="active_issue",
            error_type=_code(active.get("error_type")),
            first_seen=_optional_timestamp(active.get("first_seen")),
            last_seen=_optional_timestamp(active.get("last_seen")),
            delivery_state=delivery,
        )
    except EvidenceError as exc:
        return QueueIssueHealth.model_validate({"state": exc.state})


def _bind_source(root: Path, source: object, checked: datetime) -> list[str]:
    if not isinstance(source, str) or not source:
        raise EvidenceError("source_missing", "missing")
    manifest = Path(source)
    if not manifest.is_absolute():
        manifest = root / manifest
    try:
        resolved = manifest.resolve()
        allowed = root / ".pipeline" / "batches"
        if not resolved.is_relative_to(allowed):
            raise EvidenceError("source_outside_batches")
        if not resolved.is_file():
            raise EvidenceError("manifest_missing")
        changed = resolved.stat().st_mtime > checked.timestamp()
    except (OSError, RuntimeError) as exc:
        raise EvidenceError("manifest_unreadable") from exc
    return ["manifest_changed_since_observation"] if changed else []


def _presentation_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    return value.strip()


def _applier_label(index: int) -> str:
    label = ""
    while index >= 0:
        index, digit = divmod(index, 26)
        label = chr(ord("A") + digit) + label
        index -= 1
    return label


def _registered_controller(root: Path, manifest: Path, controller: str) -> None:
    path = (root / ".pipeline" / "blockers.sqlite3").resolve()
    if not path.is_relative_to(root):
        raise EvidenceError("controller_registry_outside_runtime")
    if not path.is_file():
        return
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT controller FROM dq_scopes WHERE scope=? LIMIT 2",
                (str(manifest),),
            ).fetchall()
    except sqlite3.Error as exc:
        raise EvidenceError("controller_registry_unavailable") from exc
    if rows != [(controller,)]:
        raise EvidenceError("controller_registration_mismatch")


def _manifest_candidates(candidates: object) -> tuple[dict[int, str], list[str]]:
    if not isinstance(candidates, list):
        return {}, []
    if len(candidates) > _JOB_LIMIT:
        raise EvidenceError("manifest_candidate_limit")
    labels: dict[int, str] = {}
    companies: list[str] = []
    for index, value in enumerate(candidates):
        candidate = _object(value)
        job_id = candidate.get("id")
        if type(job_id) is not int or job_id <= 0 or job_id in labels:
            raise EvidenceError("invalid_manifest_candidates")
        labels[job_id] = _applier_label(index)
        company = _presentation_text(candidate.get("company"), 100)
        if company and company not in companies:
            companies.append(company)
    return labels, companies


def _controller_handoff(
    budget: ReadBudget, manifest: Path, controller: ControllerHealth
) -> None:
    handoff = budget.object(manifest.with_name(manifest.stem + "-handoff.json"))
    if (
        handoff.get("manifest") != str(manifest)
        or handoff.get("controller", controller.thread_id) != controller.thread_id
    ):
        raise EvidenceError("controller_handoff_mismatch")
    statuses = (handoff.get("status"), handoff.get("controller_status"))
    if "stopped_by_user" in statuses:
        controller.worker_status = "stopped_by_user"
    elif handoff.get("status") == "launch_admitted":
        controller.worker_status = "launch_admitted"


def _batch_metadata(budget: ReadBudget, source: str, result: BatchHealth) -> None:
    manifest = Path(source)
    if not manifest.is_absolute():
        manifest = budget.root / manifest
    manifest = manifest.resolve()
    if not manifest.is_relative_to(budget.root / ".pipeline" / "batches"):
        raise EvidenceError("source_outside_batches")
    raw = budget.object(manifest)
    labels, companies = _manifest_candidates(raw.get("ordered_candidates"))
    for job in result.jobs:
        job.applier_label = labels.get(job.job_id)
    controller = _identifier(raw.get("controller_thread_id"))
    if not controller:
        return
    _registered_controller(budget.root, manifest, controller)
    dispatcher = _identifier(raw.get("desktop_dispatcher_thread_id"))
    if dispatcher == controller:
        dispatcher = None
    name = _presentation_text(raw.get("controller_display_name"), 160)
    if not name:
        summary = ", ".join(companies[:_COMPANY_SUMMARY_LIMIT])
        if len(companies) > _COMPANY_SUMMARY_LIMIT:
            summary += f" +{len(companies) - _COMPANY_SUMMARY_LIMIT}"
        name = f"{summary} applications" if summary else "Application controller"
    capacity = raw.get("max_parallel")
    result.controller = ControllerHealth(
        thread_id=controller,
        display_name=name,
        dispatch_mode="separate_dispatcher" if dispatcher else "controller",
        dispatcher_thread_id=dispatcher,
        max_parallel=capacity
        if type(capacity) is int and 1 <= capacity <= _JOB_LIMIT
        else None,
    )
    _controller_handoff(budget, manifest, result.controller)


def _populate_batch_metadata(
    budget: ReadBudget, source: object, result: BatchHealth
) -> None:
    if result.source_binding != "valid" or not isinstance(source, str):
        return
    try:
        _batch_metadata(budget, source, result)
    except EvidenceError as exc:
        if exc.state != "missing":
            result.issues.append(exc.code)
    except (OSError, RuntimeError, ValidationError):
        result.issues.append("controller_metadata_unavailable")


def _batch(budget: ReadBudget, directory: Path, now: datetime) -> BatchHealth:
    result = BatchHealth(
        id=directory.name,
        freshness="missing",
        queue_health=QueueIssueHealth(state="missing"),
    )
    source: object = None
    try:
        raw = budget.object(directory / "monitor.json")
        source = raw.get("source")
        if raw.get("version") != 1:
            raise EvidenceError("unsupported_monitor_version")
        checked = _timestamp(raw.get("checked_at"))
        age = (now - checked).total_seconds()
        if age < -_FUTURE_SKEW_SECONDS:
            raise EvidenceError("future_observation")
        result.checked_at = checked.isoformat()
        result.age_seconds = max(0, int(age))
        try:
            result.issues.extend(_bind_source(budget.root, source, checked))
            result.source_binding = "valid"
        except EvidenceError as exc:
            result.source_binding = "missing" if exc.state == "missing" else "invalid"
            raise
        snapshot = _object(raw.get("snapshot"))
        if _timestamp(snapshot.get("checked_at")) != checked:
            raise EvidenceError("timestamp_mismatch")
        result.counts = ObservationCounts.model_validate(snapshot.get("counts"))
        target = snapshot.get("target")
        complete = snapshot.get("complete")
        if type(target) is not int or target < 0 or type(complete) is not bool:
            raise EvidenceError("invalid_snapshot")
        jobs = snapshot.get("jobs")
        if not isinstance(jobs, list):
            raise EvidenceError("invalid_jobs")
        result.jobs = [_observed_job(job) for job in jobs[:_JOB_LIMIT]]
        result.jobs_truncated = len(jobs) > _JOB_LIMIT
        result.target, result.complete = target, complete
        result.blockers = _blockers(snapshot.get("blockers"))
        result.issues.extend(_snapshot_issues(snapshot))
        changed = "manifest_changed_since_observation" in result.issues
        result.freshness = "fresh" if age <= _FRESH_SECONDS and not changed else "stale"
    except EvidenceError as exc:
        result.freshness = cast(Literal["missing", "invalid", "unavailable"], exc.state)
        result.issues.append(exc.code)
    except (ValidationError, TypeError, ValueError):
        result.freshness = "invalid"
        result.issues.append("invalid_snapshot")
    _populate_batch_metadata(budget, source, result)
    result.queue_health = _queue_issue(
        budget, directory, source if result.source_binding == "valid" else None
    )
    return result


def _hold(raw: object) -> QueueHold | None:
    """The active hold the pipeline keeps in a queue row. A cleared hold is history."""
    if not isinstance(raw, str):
        return None
    value = json.loads(raw)
    return None if value.get("cleared_at") else QueueHold.model_validate(value)


def _queue_rows(connection: sqlite3.Connection) -> tuple[list[QueueJob], str | None]:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(application_queue)")
    }
    owner = (
        "codex_application_session_id"
        if "codex_application_session_id" in columns
        else "NULL"
    )
    priority = "manual_priority" if "manual_priority" in columns else "0"
    hold = "hold" if "hold" in columns else "NULL"
    records = connection.execute(
        "SELECT job_id, state, retry_count, next_retry_at, "
        f"{priority} AS manual_priority, {hold} AS hold, "
        f"{owner} AS owner_thread_id FROM application_queue "
        "ORDER BY manual_priority DESC, rowid LIMIT ?",
        (_JOB_LIMIT,),
    ).fetchall()
    jobs = [
        QueueJob(
            job_id=row["job_id"],
            queue_state=_queue_state(row["state"]) or "unknown",
            manual_priority=bool(row["manual_priority"]),
            hold=_hold(row["hold"]),
            retry_count=row["retry_count"],
            next_retry_at=_optional_timestamp(row["next_retry_at"]),
            owner_thread_id=_identifier(row["owner_thread_id"]),
        )
        for row in records
    ]
    try:
        for job in jobs:
            metadata = connection.execute(
                "SELECT substr(company, 1, 300) AS company, "
                "substr(title, 1, 500) AS title FROM jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
            if metadata:
                job.company, job.title = metadata["company"], metadata["title"]
    except sqlite3.Error:
        return jobs, "job_metadata_unavailable"
    return jobs, None


def read_queue(db_path: Path) -> QueueHealth:
    """Read bounded queue observations without creating or updating the database.

    Args:
        db_path: Existing Jobfeed SQLite database.

    Returns:
        Queue counts and bounded rows, or a missing or unavailable observation.
    """
    try:
        if not db_path.is_file():
            return QueueHealth(state="missing", error="database_missing")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='application_queue'"
            ).fetchone()
            if not exists:
                return QueueHealth(state="missing", error="queue_not_initialized")
            counts = {
                _queue_state(row["state"]) or "unknown": int(row["n"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS n FROM application_queue GROUP BY state"
                )
            }
            jobs, error = _queue_rows(connection)
            total = sum(counts.values())
            return QueueHealth(
                state="available",
                total=total,
                counts_by_state=counts,
                jobs=jobs,
                truncated=total > len(jobs),
                error=error,
            )
    except (OSError, sqlite3.Error, RuntimeError):
        return QueueHealth(state="unavailable", error="database_unreadable")
    except (EvidenceError, ValidationError, TypeError, ValueError):
        return QueueHealth(state="unavailable", error="invalid_queue_record")


@dataclass(frozen=True)
class ApplyHealthReader:
    """Project saved runtime observations without invoking workers or agents."""

    root: Path
    source: Literal["combined_sibling", "explicit_override"] = "combined_sibling"

    def confirmation(self, db_path: Path, job_id: int) -> ConfirmationEvidence:
        """Load and validate a job's saved submission confirmation evidence.

        The application folder comes from the queue row, or from the saved
        submission closeout when the queue has none, and must stay inside
        `applications/` without symbolic links.

        Args:
            db_path: Jobfeed database holding the job and its queue row.
            job_id: Job whose confirmation should be read.

        Returns:
            The validated confirmation evidence.

        Raises:
            ValueError: If the job ID or root is invalid, the job is unknown, the
                closeout names another job, or the folder is missing or escapes
                `applications/`.
        """

        if job_id <= 0 or not self.root.is_absolute():
            raise ValueError("confirmation_unavailable")
        root = self.root.resolve()
        with closing(
            sqlite3.connect(
                db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            job = connection.execute(
                "SELECT url, company FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None or not isinstance(job["url"], str):
                raise ValueError("job_not_found")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(application_queue)")
            }
            row = (
                connection.execute(
                    "SELECT application_folder_relpath FROM application_queue "
                    "WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if "application_folder_relpath" in columns
                else None
            )
            folder = row[0] if row else None
        if not folder:
            pointer = root / ".pipeline" / "submission-closeouts" / f"{job_id}.json"
            self._confined_path(root, pointer)
            saved = ReadBudget(root).object(pointer)
            if saved.get("job_id") != job_id:
                raise ValueError("job_mismatch")
            folder = saved.get("folder")
        if not isinstance(folder, str) or not folder or Path(folder).is_absolute():
            raise ValueError("confirmation_folder_missing")
        applications = root / "applications"
        target = applications / folder
        self._confined_path(root, target)
        if (
            not target.resolve().is_relative_to(applications.resolve())
            or target.resolve() == applications.resolve()
        ):
            raise ValueError("confirmation_folder_invalid")
        return read_confirmation(target, job_id, job["url"], company=job["company"])

    @staticmethod
    def _confined_path(root: Path, path: Path) -> None:
        if not path.resolve().is_relative_to(root):
            raise ValueError("confirmation_path_invalid")
        cursor = path
        while cursor != root:
            if cursor.is_symlink():
                raise ValueError("confirmation_path_invalid")
            cursor = cursor.parent

    def confirmation_response(self, db_path: Path, job_id: int) -> ConfirmationResponse:
        """Summarize a job's confirmation evidence for the dashboard.

        Args:
            db_path: Jobfeed database holding the job and its queue row.
            job_id: Job whose confirmation should be read.

        Returns:
            The evidence's kind and provenance, or an unverified status when no
            valid evidence can be read.
        """
        try:
            evidence = self.confirmation(db_path, job_id)
        except (ValueError, OSError, RuntimeError, sqlite3.Error, EvidenceError):
            return ConfirmationResponse(
                job_id=job_id,
                status="unverified",
                reason=(
                    "No valid saved confirmation evidence is available. "
                    "Do not resubmit automatically."
                ),
            )
        return ConfirmationResponse(
            job_id=job_id,
            status="available",
            kind=evidence.kind,
            captured_at=evidence.captured_at,
            source_url=evidence.source_url,
            confirmation_text=evidence.confirmation_text,
            capture_tool=evidence.capture_tool,
            source_id=evidence.source_id,
            email=evidence.email,
            artifact_url=f"/api/apply-health/jobs/{job_id}/confirmation/artifact"
            if evidence.kind == "website_screenshot"
            else None,
        )

    def _observed_members(self, budget: ReadBudget, batch: BatchHealth) -> set[int]:
        if batch.source_binding != "valid":
            return set()
        try:
            monitor = budget.object(
                budget.root / "monitor-output" / batch.id / "monitor.json"
            )
            source = monitor.get("source")
            if not isinstance(source, str):
                return set()
            manifest = Path(source)
            if not manifest.is_absolute():
                manifest = budget.root / manifest
            self._confined_path(budget.root, manifest)
            if not manifest.resolve().is_relative_to(budget.root / ".pipeline/batches"):
                return set()
            raw = budget.object(manifest)
            labels, _ = _manifest_candidates(raw.get("ordered_candidates"))
            return set(labels)
        except (EvidenceError, ValueError, OSError, RuntimeError):
            return set()

    def _completed_closeout(
        self, budget: ReadBudget, db_path: Path, job: ObservedJob
    ) -> tuple[bool, str | None]:
        """Validate exact completion records, including the native tab inventory."""
        pointer = budget.root / ".pipeline/submission-closeouts" / f"{job.job_id}.json"
        try:
            self._confined_path(budget.root, pointer)
            closeout = budget.object(pointer)
            if closeout.get("job_id") != job.job_id:
                return False, None
            folder = closeout.get("folder")
            if not isinstance(folder, str) or not folder or Path(folder).is_absolute():
                return False, None
            target = budget.root / "applications" / folder
            self._confined_path(budget.root, target)
            if (
                not target.resolve().is_relative_to(budget.root / "applications")
                or target.resolve() == budget.root / "applications"
            ):
                return False, None
            # The current queue folder, when present, must match the closeout.
            with closing(
                sqlite3.connect(
                    db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
            ) as db:
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(application_queue)")
                }
                owner_column = (
                    "codex_application_session_id"
                    if "codex_application_session_id" in columns
                    else "NULL"
                )
                row = (
                    db.execute(
                        f"SELECT application_folder_relpath,{owner_column} FROM application_queue WHERE job_id=?",
                        (job.job_id,),
                    ).fetchone()
                    if "application_folder_relpath" in columns
                    else None
                )
            if row and row[0] != folder:
                return False, None
            receipt = budget.object(target / "submission-receipt.json")
            if (
                receipt.get("job_id") != job.job_id
                or receipt.get("job_url") != job.job_url
            ):
                return False, None
            owner = _identifier(closeout.get("thread_id"))
            if not owner:
                return False, None
            if row and row[1] and row[1] != owner:
                return False, None
            if (
                closeout.get("complete") is not True
                or receipt.get("tabs_closed") is not True
            ):
                return False, owner
            evidence_name = receipt.get("tab_close_evidence")
            if not isinstance(evidence_name, str) or Path(evidence_name).is_absolute():
                return False, owner
            evidence_path = target / evidence_name
            self._confined_path(budget.root, evidence_path)
            if not evidence_path.resolve().is_relative_to(target.resolve()):
                return False, owner
            evidence = budget.object(evidence_path)
            ids = evidence.get("closed_tab_ids")
            receipt_ids = receipt.get("closed_tab_ids")
            capture = evidence.get("capture")
            valid = (
                evidence.get("job_id") == job.job_id
                and evidence.get("job_url") == job.job_url
                and evidence.get("verified_absent") is True
                and evidence.get("unknown_tab_ids") == []
                and evidence.get("global_inventory_errors") == []
                and evidence.get("same_profile_present") is True
                and isinstance(ids, list)
                and bool(ids)
                and all(isinstance(value, str) and value for value in ids)
                and isinstance(receipt_ids, list)
                and set(ids) == set(receipt_ids)
                and isinstance(capture, dict)
                and isinstance(capture.get("tool"), str)
                and bool(capture["tool"].strip())
                and capture.get("source_id") in ids
            )
            if not valid:
                return False, owner
            _timestamp(evidence.get("recorded_at"))
            return True, owner
        except (
            EvidenceError,
            ValueError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
        ):
            return False, None

    def _confirm_submissions(
        self,
        db_path: Path,
        batches: list[BatchHealth],
        budget: ReadBudget | None = None,
    ) -> None:
        """Mark jobs submitted, synced and closed only from verified evidence.

        A job counts as submitted only with valid confirmation evidence, as synced
        only when Jobfeed also records it as applied, and as closed only with a
        completed closeout. Batch counts and completion are recomputed from those
        flags. Database read failures leave the applied set empty.

        Time complexity: O(N) for N jobs across `batches`, plus one applied query
        per `_JOB_LIMIT` distinct job IDs, one confirmation read per distinct
        eligible job and one member read per batch.

        Args:
            db_path: Jobfeed database holding applied records.
            batches: Batch health rows to update in place.
            budget: Shared read budget, or None to start a new one.
        """
        budget = budget or ReadBudget(self.root.resolve())
        identifiers = list(
            dict.fromkeys(
                job.job_id
                for batch in batches
                if batch.source_binding == "valid"
                for job in batch.jobs
            )
        )
        applied = set()
        with (
            suppress(sqlite3.Error, OSError, ValueError),
            closing(
                sqlite3.connect(
                    db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
            ) as db,
        ):
            db.execute("PRAGMA query_only=ON")
            if identifiers:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('applied','real_job_applications')"
                    )
                }
                predicates = []
                if "applied" in tables:
                    predicates.append(
                        "EXISTS (SELECT 1 FROM applied a WHERE a.job_id=j.id)"
                    )
                if "real_job_applications" in tables:
                    predicates.append(
                        "EXISTS (SELECT 1 FROM real_job_applications a WHERE a.source_job_id=j.id OR a.source_applied_job_id=j.id)"
                    )
                for offset in range(0, len(identifiers) if predicates else 0, _JOB_LIMIT):
                    selected = identifiers[offset : offset + _JOB_LIMIT]
                    placeholders = ",".join("?" for _ in selected)
                    query = f"SELECT j.id FROM jobs j WHERE j.id IN ({placeholders}) AND ({' OR '.join(predicates)})"
                    applied.update(row[0] for row in db.execute(query, selected))
        confirmations: dict[int, bool] = {}
        for batch in batches:
            members = self._observed_members(budget, batch)
            for job in batch.jobs:
                claimed = job.submitted or job.user_submitted or job.synced
                if (
                    batch.source_binding == "valid"
                    and job.job_id in identifiers
                    and job.job_id in members
                ):
                    if job.job_id not in confirmations:
                        confirmations[job.job_id] = (
                            self.confirmation_response(db_path, job.job_id).status
                            == "available"
                        )
                    verified = confirmations[job.job_id]
                else:
                    verified = False
                job.submitted = verified
                job.synced = (
                    verified and job.job_id in applied and job.job_id in members
                )
                job.closed = False
                if job.synced:
                    job.closed, owner = self._completed_closeout(budget, db_path, job)
                    if owner:
                        job.owner_thread_id = owner
                if job.closed:
                    job.queue_state = "Submitted"
                    job.paused = False
                    job.blocked = None
                    job.next_step = "完成，禁止重投"
                    job.errors = [
                        error
                        for error in job.errors
                        if error
                        not in {"submission_unverified", "close_evidence_missing"}
                    ]
                elif (claimed or job.job_id in applied) and not verified:
                    if "submission_unverified" not in job.errors:
                        job.errors.append("submission_unverified")
                if job.job_id not in identifiers:
                    batch.complete = False
                    batch.issues.append("confirmation_read_limit")
            batch.issues = list(dict.fromkeys(batch.issues))
            if batch.counts:
                for field in ("submitted", "synced", "closed"):
                    setattr(
                        batch.counts,
                        field,
                        sum(getattr(job, field) and job.counted for job in batch.jobs),
                    )
                batch.counts.paused = sum(job.paused for job in batch.jobs)
                batch.counts.reserved = sum(
                    job.counted
                    and not job.terminal
                    and not job.submitted
                    and bool(
                        job.user_submitted
                        or (
                            job.owner_thread_id
                            and (
                                not job.paused
                                or job.blocked == "submission_unknown"
                                or job.turn_state not in {"ended", "not_started"}
                            )
                        )
                    )
                    for job in batch.jobs
                )
                batch.counts.occupied = sum(
                    bool(job.owner_thread_id)
                    and not job.closed
                    and job.turn_state not in {"ended", "not_started"}
                    for job in batch.jobs
                )
                target = batch.target or 0
                batch.complete = bool(
                    target > 0
                    and not batch.jobs_truncated
                    and batch.counts.submitted >= target
                    and batch.counts.synced >= target
                    and batch.counts.closed >= target
                    and not batch.counts.occupied
                    and not any(
                        job.blocked == "submission_unknown" for job in batch.jobs
                    )
                )
            if batch.jobs_truncated:
                batch.issues.append("confirmation_count_limited_to_visible_jobs")

    def read(
        self, db_path: Path, *, now: datetime | None = None
    ) -> ApplyHealthResponse:
        """Read the queue and bounded saved batch observations.

        Args:
            db_path: Existing Jobfeed database to inspect in read-only mode.
            now: Reference time for freshness, or the current UTC time.

        Returns:
            Allowlisted observations with explicit availability and freshness.
        """
        now = now or datetime.now(UTC)
        result = ApplyHealthResponse(
            checked_at=now.isoformat(),
            runtime=RuntimeHealth(state="available", source=self.source),
            queue=read_queue(db_path),
            monitoring=MonitoringHealth(state="missing"),
        )
        if not self.root.is_absolute():
            result.runtime.state = "unavailable"
            result.runtime.error = "apply_root_must_be_absolute"
            result.monitoring.state = "unavailable"
            return result
        try:
            root = self.root.resolve()
            if not root.is_dir():
                result.runtime.state = "missing"
                result.runtime.error = "apply_runtime_missing"
                return result
            monitor_root = (root / "monitor-output").resolve()
            if not monitor_root.is_relative_to(root):
                result.monitoring.state = "unavailable"
                result.monitoring.error = "monitor_output_unreadable"
                return result
            directories, truncated = _directories(monitor_root)
            budget = ReadBudget(root)
            result.batches = [_batch(budget, path, now) for path in directories]
            _populate_job_urls(db_path, result.batches)
            self._confirm_submissions(db_path, result.batches, budget)
            result.monitoring = MonitoringHealth(
                state="available",
                discovered_batches=len(directories),
                truncated=truncated,
            )
        except FileNotFoundError:
            result.monitoring.error = "monitor_output_missing"
        except (OSError, RuntimeError, EvidenceError):
            result.monitoring.state = "unavailable"
            result.monitoring.error = "monitor_output_unreadable"
        return result


def _observation_mtime(path: Path, root: Path) -> float:
    try:
        monitor = (path / "monitor.json").resolve()
        if not monitor.is_relative_to(root):
            return 0
        return monitor.stat().st_mtime
    except (OSError, RuntimeError):
        return path.lstat().st_mtime


def _directories(root: Path) -> tuple[list[Path], bool]:
    directories: list[Path] = []
    truncated = False
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= _DIRECTORY_LIMIT:
                truncated = True
                break
            if entry.is_dir():
                directories.append(Path(entry.path))
    directories.sort(key=lambda path: (-_observation_mtime(path, root), path.name))
    return directories[:_BATCH_LIMIT], truncated or len(directories) > _BATCH_LIMIT
