from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class QueueAdmission:
    status: Literal[
        "queued",
        "already_queued",
        "already_applied",
        "needs_details",
        "closed",
    ]
    source_job_id: int
    real_job_id: int | None = None
    queue_job_id: int | None = None
    queue_state: str | None = None
    reason: str | None = None
    manual_priority: bool = False
    identity_warnings: tuple[str, ...] = ()
    related_source_job_ids: tuple[int, ...] = ()


def admit_job(connection: sqlite3.Connection, job_id: int, *, manual_priority: bool = False) -> QueueAdmission:
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _admit_locked(connection, job_id, manual_priority=manual_priority)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def _admit_locked(connection: sqlite3.Connection, job_id: int, *, manual_priority: bool) -> QueueAdmission:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    canonical = "real_jobs" in tables
    real_job_id = None
    siblings = [job_id]
    job = None
    if "jobs" in tables:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return QueueAdmission(
                "needs_details", job_id, reason="Source job does not exist"
            )
        if "real_job_id" in job.keys():
            real_job_id = job["real_job_id"]
            if real_job_id is not None:
                siblings = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM jobs WHERE real_job_id=?", (real_job_id,)
                    )
                ]
    related_ids = tuple(sorted(source_id for source_id in siblings if source_id != job_id))
    warnings = ["related_sources"] if related_ids else []
    existing = connection.execute(
        "SELECT job_id,state,manual_priority FROM application_queue WHERE job_id=?", (job_id,)
    ).fetchone()
    if existing and manual_priority:
        connection.execute("UPDATE application_queue SET manual_priority=1 WHERE job_id=?", (job_id,))
    if related_ids:
        values = ",".join("?" for _ in related_ids)
        if connection.execute(
            f"SELECT 1 FROM application_queue WHERE job_id IN ({values}) LIMIT 1",
            related_ids,
        ).fetchone():
            warnings.append("related_source_queued")
        if "applied" in tables and connection.execute(
            f"SELECT 1 FROM applied WHERE job_id IN ({values}) LIMIT 1", related_ids
        ).fetchone():
            warnings.append("related_source_applied")
    source_applied = "applied" in tables and connection.execute(
        "SELECT 1 FROM applied WHERE job_id=? LIMIT 1", (job_id,)
    ).fetchone() is not None
    if "real_job_applications" in tables:
        source_applied = source_applied or connection.execute(
            "SELECT 1 FROM real_job_applications WHERE source_job_id=? "
            "OR source_applied_job_id=? LIMIT 1", (job_id, job_id)
        ).fetchone() is not None
        if connection.execute(
            "SELECT 1 FROM real_job_applications WHERE real_job_id=? LIMIT 1",
            (real_job_id,),
        ).fetchone():
            warnings.append("canonical_application_recorded")
        if related_ids and connection.execute(
            f"SELECT 1 FROM real_job_applications WHERE source_job_id IN ({values}) "
            f"OR source_applied_job_id IN ({values}) LIMIT 1",
            [*related_ids, *related_ids],
        ).fetchone() and "related_source_applied" not in warnings:
            warnings.append("related_source_applied")
    workflow = None
    if "real_job_status" in tables:
        workflow = connection.execute(
            "SELECT status FROM real_job_status WHERE real_job_id=?", (real_job_id,)
        ).fetchone()
        if workflow and workflow["status"] == "applied":
            warnings.append("canonical_status_applied")
    parent = None
    if canonical:
        parent = connection.execute(
            "SELECT * FROM real_jobs WHERE id=?", (real_job_id,)
        ).fetchone()
        if parent is not None:
            if parent["identity_review_state"] != "clear":
                warnings.append("identity_review:" + str(parent["identity_review_state"]))
            if parent["official_closed_at"] and not job["closed_at"]:
                warnings.append("canonical_closed")
    fields = dict(
        source_job_id=job_id,
        real_job_id=real_job_id,
        manual_priority=bool(existing and (existing["manual_priority"] or manual_priority)),
        queue_job_id=existing["job_id"] if existing else None,
        queue_state=existing["state"] if existing else None,
        identity_warnings=tuple(warnings),
        related_source_job_ids=related_ids,
    )
    if source_applied:
        return QueueAdmission("already_applied", **fields)
    if workflow and workflow["status"] == "ignored":
        return QueueAdmission(
            "needs_details",
            **fields,
            reason="Job was explicitly ignored. Restore it in Jobfeed before queueing.",
        )
    if existing:
        return QueueAdmission(
            "already_queued", **fields, reason="Existing queue state is preserved"
        )
    if canonical:
        if parent is None:
            return QueueAdmission(
                "needs_details", **fields, reason="Canonical job record is missing"
            )
        if job["closed_at"]:
            return QueueAdmission("closed", **fields, reason="Posting is closed")
        if not all(
            job[field] and str(job[field]).strip()
            for field in ("company", "title", "jd_text", "url")
        ):
            return QueueAdmission(
                "needs_details",
                **fields,
                reason="A complete title, company, description, and posting URL are required",
            )
    connection.execute(
        "INSERT INTO application_queue(job_id,state,manual_priority) VALUES (?,?,?)", (job_id, "Queued", int(manual_priority))
    )
    fields.update(queue_job_id=job_id, queue_state="Queued", manual_priority=manual_priority)
    return QueueAdmission("queued", **fields)
