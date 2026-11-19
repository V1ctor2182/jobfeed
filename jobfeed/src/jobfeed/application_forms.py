"""Read bounded form journals without invoking application services."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import stat
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

from jobfeed.apply_health import ApplyHealthReader, EvidenceError, ReadBudget
from jobfeed.services.application_form_models import (
    ApplicationFormResponse,
    FormField,
    FormResume,
    FormSnapshot,
    FormValue,
)

MAX_RECORDS = 300
MAX_FIELDS = 1000
MAX_HISTORY_FIELDS = 10000
MAX_VALUE = 30000
MAX_PDF = 20 * 1024 * 1024
SENSITIVE = re.compile(
    r"password|passcode|one[\s_.-]*time|verification[\s_.-]*code|"
    r"security[\s_.-]*code|auth(?:entication)?[\s_.-]*(?:code|token)|"
    r"access[\s_.-]*token|api[\s_.-]*key|secret|token|"
    r"(?:^|[\W_])otp(?:$|[\W_])|captcha|验证码|密码",
    re.I,
)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: object, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid_records")
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= MAX_VALUE else None


def _time(value: object) -> str | None:
    text = _text(value)
    if text:
        with suppress(ValueError):
            return (
                text
                if datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo
                else None
            )
    return None


def _url(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return None


def _value(value: object) -> FormValue:
    if value is None or type(value) is bool or type(value) is int:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        masked = re.search(r"<redacted>|\[redacted\]|<masked>", value, re.I)
        return value if len(value) <= MAX_VALUE and not masked else None
    if (
        isinstance(value, list)
        and len(value) <= MAX_FIELDS
        and all(isinstance(item, str) and _value(item) is not None for item in value)
    ):
        return value
    return None


def _field(raw: object, page_url: str | None, *, captured: bool) -> FormField | None:
    row = _object(raw)
    question = _text(row.get("label") if captured else row.get("question"))
    field_id = _text(row.get("fieldId"))
    if not question or not field_id:
        return None
    if SENSITIVE.search(" ".join((question, field_id, str(row.get("type", ""))))):
        return None
    if row.get("reason") == "sensitive_or_action_control":
        return None
    if row.get("type") in {"hidden", "submit", "button", "reset"}:
        return None
    actual = _value(row.get("value") if captured else row.get("verifiedAnswer"))
    written = None if captured else _value(row.get("writtenAnswer"))
    state: Literal["observed", "written_unverified", "unanswered", "unavailable"] = (
        "unavailable"
    )
    if not captured and row.get("verification") != "verified":
        actual = None
    if row.get("truncated") is True:
        actual = None
    elif captured and (row.get("status") == "empty" or actual in ("", [])):
        state = "unanswered"
    elif actual is not None and (captured or row.get("verification") == "verified"):
        state = "observed"
    elif written is not None:
        state = "written_unverified"
    return FormField(
        id=field_id,
        question=question,
        value=actual,
        state=state,
        written_value=written,
        page_url=page_url,
        section=_text(row.get("section")),
    )


def _fields(raw: object, page_url: str | None, *, captured: bool) -> list[FormField]:
    return [
        field
        for row in _records(raw, MAX_FIELDS)
        if (field := _field(row, page_url, captured=captured))
    ]


@dataclass
class FormContext:
    """Paths, run identity and read budget for one application run's saved form."""

    root: Path
    folder: Path
    scope: str
    batch_id: str
    job_id: int
    owner: str | None
    receipt: dict[str, Any]
    budget: ReadBudget
    pdf_bytes: bytes | None = None

    def read(self, filename: str) -> dict[str, Any]:
        """Read one JSON file from the run folder, confined to the pipeline root.

        Args:
            filename: File name inside the run folder.

        Returns:
            The parsed JSON object.
        """
        path = self.folder / filename
        ApplyHealthReader._confined_path(self.root, path)
        return self.budget.object(path)


def _folder(root: Path, database: Path, job_id: int, budget: ReadBudget) -> Path:
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("PRAGMA query_only=ON")
        if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise ValueError("unknown_job")
        row = db.execute(
            "SELECT application_folder_relpath FROM application_queue WHERE job_id=?",
            (job_id,),
        ).fetchone()
    relative = row[0] if row else None
    if not relative:
        pointer = root / ".pipeline/submission-closeouts" / f"{job_id}.json"
        ApplyHealthReader._confined_path(root, pointer)
        closeout = budget.object(pointer)
        if closeout.get("job_id") != job_id:
            raise ValueError("wrong_job")
        relative = closeout.get("folder")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("invalid_folder")
    folder = root / "applications" / relative
    ApplyHealthReader._confined_path(root, folder)
    if not folder.resolve().is_relative_to(root / "applications"):
        raise ValueError("invalid_folder")
    if folder.resolve() == root / "applications":
        raise ValueError("invalid_folder")
    return folder


def _context(root: Path, database: Path, job_id: int, batch_id: str) -> FormContext:
    if (
        not root.is_absolute()
        or job_id < 1
        or not re.fullmatch(r"[\w.-]{1,200}", batch_id)
    ):
        raise ValueError("invalid_request")
    if batch_id in {".", ".."}:
        raise ValueError("invalid_batch")
    root = root.resolve()
    budget = ReadBudget(root)
    monitor_path = root / "monitor-output" / batch_id / "monitor.json"
    ApplyHealthReader._confined_path(root, monitor_path)
    monitor = budget.object(monitor_path)
    source = monitor.get("source")
    if not isinstance(source, str):
        raise ValueError("invalid_scope")
    scope = Path(source) if Path(source).is_absolute() else root / source
    ApplyHealthReader._confined_path(root, scope)
    if not scope.resolve().is_relative_to(root / ".pipeline/batches"):
        raise ValueError("invalid_scope")
    manifest = budget.object(scope)
    candidates = manifest.get("ordered_candidates")
    if not isinstance(candidates, list) or job_id not in {
        row.get("id") for row in candidates if isinstance(row, dict)
    }:
        raise ValueError("job_outside_batch")
    observations = _object(monitor.get("snapshot")).get("jobs", [])
    owner = (
        next(
            (
                _text(row.get("owner"))
                for row in observations
                if isinstance(row, dict) and row.get("job_id") == job_id
            ),
            None,
        )
        if isinstance(observations, list)
        else None
    )
    context = FormContext(
        root,
        _folder(root, database, job_id, budget),
        str(scope.resolve()),
        batch_id,
        job_id,
        owner,
        {},
        budget,
    )
    try:
        receipt = context.read("resume-source.json")
    except EvidenceError as error:
        if error.state != "missing":
            raise
        receipt = {}
    if receipt.get("job_id") == job_id and receipt.get("scope") == context.scope:
        context.receipt = receipt
    return context


def _pdf(context: FormContext) -> tuple[bytes, str]:
    relative = _object(_object(context.receipt.get("assets")).get("pdf")).get("path")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("resume_unavailable")
    path = context.folder / relative
    ApplyHealthReader._confined_path(context.folder.resolve(), path)
    if (
        not path.resolve().is_relative_to(context.folder.resolve())
        or path.suffix.lower() != ".pdf"
    ):
        raise ValueError("resume_unavailable")
    if context.pdf_bytes is not None:
        return context.pdf_bytes, path.name
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("resume_unavailable")
        data = stream.read(MAX_PDF + 1)
    if len(data) > MAX_PDF or not data.startswith(b"%PDF-"):
        raise ValueError("resume_unavailable")
    context.pdf_bytes = data
    return data, path.name


def _resume(
    context: FormContext, run: dict[str, Any], snapshot: FormSnapshot
) -> FormResume | None:
    selected = _object(run.get("selectedResume"))
    if snapshot.provenance == "captured":
        if not selected or selected.get("ownerThreadId") != snapshot.owner_thread_id:
            return None
        filename = _text(selected.get("filename"))
        label = _text(selected.get("label"))
        bound = selected.get("resumeId") == context.receipt.get("resume_id")
        bound = bound and selected.get("pdfPath") == _object(
            _object(context.receipt.get("assets")).get("pdf")
        ).get("path")
    elif snapshot.id == "resume-selection":
        if not context.receipt:
            return None
        filename = _text(
            _object(_object(context.receipt.get("assets")).get("pdf")).get("path")
        )
        label = _text(context.receipt.get("label"))
        bound = True
    else:
        return None
    if not filename:
        return None
    result = FormResume(
        filename=Path(filename).name, label=label, upload_status="selected"
    )
    _upload_evidence(run, result)
    if bound:
        try:
            _pdf(context)
        except (OSError, ValueError, RuntimeError):
            return result
        result.artifact_url = (
            f"/api/apply-health/jobs/{context.job_id}/form/resume?"
            + urlencode({"batch_id": context.batch_id, "snapshot_id": snapshot.id})
        )
    return result


def _upload_evidence(run: dict[str, Any], result: FormResume) -> None:
    for raw in run.get("fields", []) if isinstance(run.get("fields"), list) else []:
        field = _object(raw)
        if field.get("fieldId") != "mapped:resume":
            continue
        if field.get("verifiedAnswer") != result.filename:
            continue
        if field.get("uploadStatus") == "uploaded":
            result.upload_status = "uploaded"
        elif field.get("uploadStatus") == "selected_pending_submission":
            result.upload_status = "upload_pending"


def _identity(
    context: FormContext, capture: dict[str, Any]
) -> tuple[str | None, str | None] | None:
    if capture.get("jobId") != context.job_id:
        return None
    owner = _text(capture.get("ownerThreadId"))
    scope = capture.get("scope")
    if scope != context.scope and not (
        scope is None and owner and owner == context.owner
    ):
        return None
    return owner, _text(capture.get("attemptId"))


@dataclass
class SavedPage:
    """Fields captured from one form page and whether the capture is complete."""

    fields: list[FormField]
    complete: bool


def _page_fields(
    observed_pages: dict[str, SavedPage],
    run: dict[str, Any],
    capture: dict[str, Any],
    index: int,
) -> tuple[list[FormField], bool] | None:
    url = _url(run.get("pageUrl"))
    fields = _fields(
        capture.get("fields", []) if capture else run.get("fields", []),
        url,
        captured=bool(capture),
    )
    if not fields and not capture:
        return None
    raw_page_key = _text(run.get("pageKey"))
    page_key = (
        "|".join(raw_page_key.split("|")[:2])
        if raw_page_key
        else url or f"page-{index}"
    )
    captured_at = _time(capture.get("capturedAt") or run.get("at"))
    for field in fields:
        field.captured_at = captured_at
    if capture and capture.get("coverage") == "visible_controls":
        complete = not capture.get("truncated") and not any(
            _object(row).get("truncated") for row in capture.get("fields", [])
        )
        observed_pages[page_key] = SavedPage(fields, complete)
    elif capture:
        previous_page = observed_pages.get(page_key, SavedPage([], False))
        observed_pages[page_key] = SavedPage(previous_page.fields, False)
    elif not capture:
        previous_page = observed_pages.get(page_key, SavedPage([], False))
        previous = {field.id: field for field in previous_page.fields}
        previous.update({field.id: field for field in fields})
        observed_pages[page_key] = SavedPage(list(previous.values()), False)
    combined = [field for page in observed_pages.values() for field in page.fields]
    if len(combined) > MAX_FIELDS:
        raise ValueError("too_many_fields")
    return combined, all(page.complete for page in observed_pages.values())


def _history(context: FormContext, journal: dict[str, Any]) -> list[FormSnapshot]:
    runs = _records(journal.get("runs", []), MAX_RECORDS)
    pages: dict[tuple[str | None, str | None, bool], dict[str, SavedPage]] = {}
    snapshots = []
    history_fields = 0
    for index, raw in enumerate(runs):
        run = _object(raw)
        capture = _object(run.get("formSnapshot"))
        if "formSnapshot" in run and not capture:
            raise ValueError("invalid_form_snapshot")
        identity = _identity(context, capture) if capture else (None, None)
        if identity is None or (not capture and not context.receipt):
            continue
        key = (*identity, bool(capture))
        observed_pages = pages.setdefault(key, {})
        observation = _page_fields(observed_pages, run, capture, index)
        if observation is None:
            continue
        combined, capture_complete = observation
        history_fields += len(combined)
        if history_fields > MAX_HISTORY_FIELDS:
            raise ValueError("history_too_large")
        snapshot = FormSnapshot(
            id=f"observation-{index}",
            captured_at=_time(capture.get("capturedAt") or run.get("at")),
            owner_thread_id=identity[0],
            attempt_id=identity[1],
            provenance="captured" if capture else "legacy",
            coverage="visible_controls" if capture_complete else "partial_history",
            source_url=_url(run.get("pageUrl")),
            fields=combined,
        )
        snapshot.resume = _resume(context, run, snapshot)
        snapshots.append(snapshot)
    corrections = _records(journal.get("manualCorrections", []), MAX_FIELDS)
    if context.receipt and corrections:
        snapshot = _manual_snapshot(corrections, snapshots)
        if snapshot.fields:
            snapshots.append(snapshot)
    if context.receipt and not any(row.resume for row in snapshots):
        selection = FormSnapshot(
            id="resume-selection", provenance="legacy", coverage="partial_history"
        )
        selection.resume = _resume(context, {}, selection)
        if selection.resume:
            snapshots.insert(0, selection)
    snapshots.reverse()
    return sorted(snapshots, key=_snapshot_time, reverse=True)


def _snapshot_time(snapshot: FormSnapshot) -> float:
    return (
        datetime.fromisoformat(snapshot.captured_at.replace("Z", "+00:00")).timestamp()
        if snapshot.captured_at
        else float("-inf")
    )


def _journal(context: FormContext) -> dict[str, Any]:
    try:
        return context.read("application-answers.json")
    except EvidenceError as error:
        if error.state != "missing":
            raise
        return {}


def _manual_snapshot(
    corrections: list[Any], snapshots: list[FormSnapshot]
) -> FormSnapshot:
    legacy = next(
        (row for row in reversed(snapshots) if row.provenance == "legacy"), None
    )
    fields = (
        {(field.page_url, field.id): field for field in legacy.fields} if legacy else {}
    )
    captured_at = None
    for raw in corrections:
        row = _object(raw)
        verified = row.get("verifiedValue", row.get("verificationValue"))
        if row.get("status") not in {
            "verified",
            "verified_visually",
            "corrected_and_verified",
            "filled_and_verified",
        }:
            verified = None
        field = _field(
            {
                "fieldId": row.get("fieldId"),
                "question": row.get("question"),
                "writtenAnswer": row.get("answer"),
                "verifiedAnswer": verified,
                "verification": "verified" if verified is not None else "unverified",
            },
            _url(row.get("url")),
            captured=False,
        )
        if field:
            fields[(field.page_url, field.id)] = field
            captured_at = (
                _time(row.get("recorded_at") or row.get("verifiedAt")) or captured_at
            )
            field.captured_at = _time(row.get("recorded_at") or row.get("verifiedAt"))
    return FormSnapshot(
        id="manual-corrections",
        captured_at=captured_at,
        provenance="legacy",
        coverage="partial_history",
        fields=list(fields.values()),
    )


def read_application_form(
    root: Path, database: Path, job_id: int, batch_id: str
) -> ApplicationFormResponse:
    """Read the saved form observations for one job in a batch.

    Args:
        root: Pipeline root.
        database: Jobfeed database used to verify the run.
        job_id: Job whose form should be read.
        batch_id: Batch the application run belongs to.

    Returns:
        The saved snapshots with their coverage, or a missing or unavailable
        status with the reason.
    """
    try:
        context = _context(root, database, job_id, batch_id)
        snapshots = _history(context, _journal(context))
    except EvidenceError as error:
        return ApplicationFormResponse(
            job_id=job_id,
            status="missing" if error.state == "missing" else "unavailable",
            reason="No readable saved form observation is available.",
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError):
        return ApplicationFormResponse(
            job_id=job_id,
            status="unavailable",
            reason="The saved form could not be verified for this application run.",
        )
    if not snapshots:
        return ApplicationFormResponse(
            job_id=job_id,
            status="missing",
            reason="No fields were captured for this application run.",
        )
    partial = snapshots[0].coverage == "partial_history"
    return ApplicationFormResponse(
        job_id=job_id,
        status="partial" if partial else "available",
        snapshots=snapshots,
        reason=(
            "This record has partial form coverage. "
            "Older records may lack a verified run identity."
        )
        if partial
        else (
            "Saved observations cover the pages visited. "
            "They do not prove submission or a complete application."
        ),
    )


def read_form_resume(
    root: Path, database: Path, job_id: int, batch_id: str, snapshot_id: str
) -> bytes:
    """Read the résumé PDF attached to one saved form snapshot.

    Args:
        root: Pipeline root.
        database: Jobfeed database used to verify the run.
        job_id: Job whose form should be read.
        batch_id: Batch the application run belongs to.
        snapshot_id: Snapshot whose résumé should be returned.

    Returns:
        The PDF bytes.

    Raises:
        ValueError: If the snapshot is unknown or has no résumé artifact.
    """
    context = _context(root, database, job_id, batch_id)
    snapshots = _history(context, _journal(context))
    selected = next((row for row in snapshots if row.id == snapshot_id), None)
    if selected is None or selected.resume is None or not selected.resume.artifact_url:
        raise ValueError("resume_unavailable")
    return _pdf(context)[0]
