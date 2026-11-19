"""How one sync run reconciles the user's Google Sheet with the recruiter sheet.

The sheet holds the only state, so every run recomputes from the cells it read and the
rows Jobfeed returns. The sync owns the job columns and the sync note, the user owns
status, recruiter and note, and a row the user wrote on is never deleted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from typing import Any

from jobfeed.domain.recruiter_sheet import (
    SheetDecision,
    SheetEdit,
    SheetRow,
    SheetStatus,
    edit_problem,
)


class Col(IntEnum):
    """The Jobs tab's columns, A to P."""

    JOB_ID = 0
    COMPANY = 1
    ROLE = 2
    LOCATION = 3
    FIT = 4
    WHY = 5
    WATCH_OUT = 6
    LINK = 7
    STATUS = 8
    RECRUITER = 9
    NOTE = 10
    UPDATED = 11
    SYNC_NOTE = 12
    PAY = 13
    FUNDING = 14
    FUNDING_SOURCE = 15


HEADER: tuple[str, ...] = (
    "Job ID",
    "Company",
    "Role",
    "Location",
    "Fit",
    "Why it fits",
    "Watch out for",
    "Link",
    "Status",
    "Recruiter",
    "Note",
    "Updated",
    "Sync note",
    "Pay",
    "Funding",
    "Funding source",
)
STATUS_LABELS: Mapping[SheetStatus, str] = {
    SheetStatus.TO_ASK: "to ask",
    SheetStatus.ASKED: "asked",
    SheetStatus.WITH_RECRUITER: "with recruiter",
    SheetStatus.PASSED: "passed",
    SheetStatus.APPLY_MYSELF: "apply myself",
}
_STATUS_BY_LABEL = {label: status.value for status, label in STATUS_LABELS.items()}
_API_WORD_FOR_STATUS = re.compile(
    r"\b(?:" + "|".join(status.value for status in STATUS_LABELS) + r")\b"
)
_UNEDITED_STATUS = STATUS_LABELS[SheetStatus.TO_ASK]
_SEPARATOR = " · "
_REFUSED = "Refused: "
_INTEGER = re.compile(r"\d[\d,]*(?:\.0+)?")
_NUMERIC = frozenset({Col.JOB_ID, Col.FIT, Col.PAY})

_SYNCED: tuple[tuple[Col, Callable[[SheetRow], str]], ...] = (
    (Col.COMPANY, lambda row: row.company),
    (Col.ROLE, lambda row: row.title),
    (Col.LOCATION, lambda row: row.location),
    (Col.FIT, lambda row: "" if row.fit is None else str(row.fit)),
    (Col.WHY, lambda row: row.why),
    (Col.WATCH_OUT, lambda row: row.watch_out),
    (Col.LINK, lambda row: row.url or ""),
    (Col.UPDATED, lambda row: (row.updated_at or "")[:10]),
    (Col.PAY, lambda row: "" if row.pay is None else str(row.pay)),
    (Col.FUNDING, lambda row: row.funding),
    (Col.FUNDING_SOURCE, lambda row: row.funding_source or ""),
)
_VERIFIED: tuple[Col, ...] = (
    Col.JOB_ID,
    *(column for column, _ in _SYNCED),
    Col.SYNC_NOTE,
)
_FACTS: tuple[tuple[str, Callable[[SheetRow], bool]], ...] = (
    ("Posting closed", lambda row: row.closed),
    ("Applied", lambda row: row.applied),
    (
        "No longer qualifies",
        lambda row: not (row.qualifies or row.closed or row.applied),
    ),
    ("Funding check pending", lambda row: row.funding_pending),
)


class RowKind(Enum):
    """What a nonblank row of the tab turned out to be."""

    LIVE = "live"
    DEPARTED = "departed"
    DUPLICATE = "duplicate"
    UNPARSABLE = "unparsable"


# A row with no job behind it. The note is its sync note when it is kept.
_STRAY_NOTES: Mapping[RowKind, str] = {
    RowKind.DEPARTED: "This job left the list, so the edit was not saved",
    RowKind.DUPLICATE: "Duplicate of another row, so the edit was not saved",
    RowKind.UNPARSABLE: "Job ID not recognized, so this row was not synced",
}
_DELETABLE = frozenset({RowKind.DEPARTED, RowKind.DUPLICATE})


@dataclass(frozen=True)
class _TabRow:
    index: int
    cells: tuple[str, ...]
    kind: RowKind
    job: SheetRow | None = None


@dataclass(frozen=True)
class EditOutcomes:
    """What became of this run's edits, by real job ID."""

    applied: frozenset[int] = frozenset()
    refused: Mapping[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncPlan:
    """One atomic batch of requests and what the tab must then hold."""

    requests: tuple[dict[str, Any], ...] = ()
    intended: tuple[tuple[str, ...], ...] = ()
    added: int = 0
    updated: int = 0
    removed: int = 0


def has_expected_header(grid: Sequence[Sequence[str]]) -> bool:
    """Check that a read tab is the one this sync knows how to write.

    Args:
        grid: The tab's rows as read, header first.

    Returns:
        Whether the first row is exactly the expected header.
    """
    return bool(grid) and tuple(grid[0]) == HEADER


def _pad(row: Sequence[str]) -> tuple[str, ...]:
    return (*row, *[""] * (len(HEADER) - len(row)))


def _source_id(text: str) -> int | None:
    stripped = text.strip()
    if not _INTEGER.fullmatch(stripped):
        return None
    return int(stripped.replace(",", "").partition(".")[0])


def _tab_rows(grid: Sequence[Sequence[str]], rows: Iterable[SheetRow]) -> list[_TabRow]:
    by_source = {source: row for row in rows for source in row.source_job_ids}
    seen: set[int] = set()
    tab_rows = []
    for index, raw in enumerate(grid[1:], start=1):
        cells = _pad(raw)
        if not any(cell.strip() for cell in cells):
            continue
        source = _source_id(cells[Col.JOB_ID])
        job = by_source.get(source) if source is not None else None
        if source is None:
            kind = RowKind.UNPARSABLE
        elif job is None:
            kind = RowKind.DEPARTED
        elif job.real_job_id in seen:
            kind = RowKind.DUPLICATE
        else:
            kind = RowKind.LIVE
            seen.add(job.real_job_id)
        tab_rows.append(
            _TabRow(index, cells, kind, job if kind is RowKind.LIVE else None)
        )
    return tab_rows


def _edit(row: _TabRow, job: SheetRow) -> SheetEdit | None:
    status_text = row.cells[Col.STATUS].strip()
    status = _STATUS_BY_LABEL.get(status_text, status_text or job.status.value)
    recruiter = row.cells[Col.RECRUITER].strip()
    note = row.cells[Col.NOTE].strip()
    if (status, recruiter, note) == (job.status.value, job.recruiter, job.note):
        return None
    return SheetEdit(job.real_job_id, status, recruiter, note)


def sheet_edits(
    grid: Sequence[Sequence[str]], rows: Iterable[SheetRow]
) -> list[SheetEdit]:
    """Find the rows whose status, recruiter or note differ from what is stored.

    Args:
        grid: The tab's rows as read, header first.
        rows: The recruiter sheet.

    Returns:
        One edit per changed row. An unknown status is passed on raw, so it is refused
        by the same check as any other bad edit.
    """
    return [
        edit
        for row in _tab_rows(grid, rows)
        if row.job is not None and (edit := _edit(row, row.job)) is not None
    ]


def preview_edits(
    rows: Iterable[SheetRow], edits: Iterable[SheetEdit]
) -> tuple[list[SheetRow], EditOutcomes]:
    """Work out what ``apply_edits`` would do, for a dry run that saves nothing.

    Args:
        rows: The recruiter sheet.
        edits: The edits found on the tab.

    Returns:
        The rows as they would then read, and the outcomes.
    """
    by_id = {row.real_job_id: row for row in rows}
    applied: set[int] = set()
    refused: dict[int, str] = {}
    for edit in edits:
        row = by_id[edit.real_job_id]
        decision = SheetDecision(row.status, row.recruiter, row.note)
        problem = edit_problem(edit, decision)
        if problem is not None:
            refused[edit.real_job_id] = problem.message
            continue
        applied.add(edit.real_job_id)
        by_id[edit.real_job_id] = replace(
            row,
            status=SheetStatus(edit.status),
            recruiter=edit.recruiter or "",
            note=edit.note or "",
        )
    return list(by_id.values()), EditOutcomes(frozenset(applied), refused)


def _canon(column: Col, text: str) -> str:
    if column is Col.STATUS:
        return text.strip()
    if column in _NUMERIC and _INTEGER.fullmatch(text.strip()):
        return str(_source_id(text))
    return text


def _existing_refusal(sync_note: str) -> str:
    start = sync_note.find(_REFUSED)
    return sync_note[start:] if start >= 0 else ""


def _in_sheet_words(message: str) -> str:
    return _API_WORD_FOR_STATUS.sub(
        lambda match: STATUS_LABELS[SheetStatus(match.group())], message
    )


def _sync_note(job: SheetRow, sync_note: str, outcomes: EditOutcomes) -> str:
    facts = [text for text, holds in _FACTS if holds(job)]
    if job.real_job_id in outcomes.refused:
        refusal = _REFUSED + _in_sheet_words(outcomes.refused[job.real_job_id])
    elif job.real_job_id in outcomes.applied:
        refusal = ""
    else:
        refusal = _existing_refusal(sync_note)
    return _SEPARATOR.join([*facts, *([refusal] if refusal else [])])


def _wanted(row: _TabRow, outcomes: EditOutcomes) -> Mapping[Col, str]:
    sync_note = row.cells[Col.SYNC_NOTE]
    if row.job is None:
        refusal = _existing_refusal(sync_note)
        parts = [_STRAY_NOTES[row.kind], *([refusal] if refusal else [])]
        return {Col.SYNC_NOTE: _SEPARATOR.join(parts)}
    return {
        **{column: value(row.job) for column, value in _SYNCED},
        Col.STATUS: STATUS_LABELS[row.job.status],
        Col.SYNC_NOTE: _sync_note(row.job, sync_note, outcomes),
    }


def _is_unedited(cells: Sequence[str]) -> bool:
    return (
        cells[Col.STATUS].strip() == _UNEDITED_STATUS
        and not cells[Col.RECRUITER].strip()
        and not cells[Col.NOTE].strip()
    )


def _cell(column: Col, text: str) -> dict[str, Any]:
    if not text:
        return {}
    if column in _NUMERIC:
        return {"userEnteredValue": {"numberValue": int(text)}}
    return {"userEnteredValue": {"stringValue": text}}


def _update_cells(
    tab_id: int, row_index: int, start_column: int, values: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "updateCells": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": row_index,
                "endRowIndex": row_index + 1,
                "startColumnIndex": start_column,
                "endColumnIndex": start_column + len(values),
            },
            "rows": [{"values": values}],
            "fields": "userEnteredValue",
        }
    }


def _delete_row(tab_id: int, row_index: int) -> dict[str, Any]:
    return {
        "deleteDimension": {
            "range": {
                "sheetId": tab_id,
                "dimension": "ROWS",
                "startIndex": row_index,
                "endIndex": row_index + 1,
            }
        }
    }


def _replace_rows(tab_id: int, end_row: int, count: int) -> dict[str, Any]:
    return {
        "insertDimension": {
            "range": {
                "sheetId": tab_id,
                "dimension": "ROWS",
                "startIndex": end_row,
                "endIndex": end_row + count,
            },
            "inheritFromBefore": True,
        }
    }


def _sort_by_fit(tab_id: int, end_row: int) -> dict[str, Any]:
    return {
        "sortRange": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": 1,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": len(HEADER),
            },
            "sortSpecs": [{"dimensionIndex": Col.FIT, "sortOrder": "DESCENDING"}],
        }
    }


def _new_row_cells(job: SheetRow) -> dict[Col, str]:
    return {
        Col.JOB_ID: str(job.source_job_ids[0]),
        **{column: value(job) for column, value in _SYNCED},
        Col.STATUS: STATUS_LABELS[job.status],
        Col.RECRUITER: job.recruiter,
        Col.NOTE: job.note,
        Col.SYNC_NOTE: _sync_note(job, "", EditOutcomes()),
    }


def _verified(cells: Sequence[str]) -> tuple[str, ...]:
    return tuple(_canon(column, cells[column]) for column in _VERIFIED)


def plan_sync(
    grid: Sequence[Sequence[str]],
    rows: Sequence[SheetRow],
    outcomes: EditOutcomes,
    tab_id: int,
) -> SyncPlan:
    """Compute the one batch that brings the tab in step with the recruiter sheet.

    Updates and appends use the coordinates of the rows as read, because they run
    first. Deletes then run highest row first, so each leaves the next row's index
    valid. New blank rows then replace the deleted ones below the data, so the tab keeps
    its size and they inherit the Status dropdown. The sort runs last, over the row
    count the deletes leave. With only the header left, no row is replaced, because a
    new row would inherit the header's styling.

    Args:
        grid: The tab's rows as read, header first.
        rows: The recruiter sheet after this run's edits.
        outcomes: What became of this run's edits.
        tab_id: The tab's grid ID.

    Returns:
        The requests, empty when the tab is already in step, and what it must then hold.
    """
    updates: list[dict[str, Any]] = []
    deletes: list[int] = []
    intended: list[tuple[str, ...]] = []
    updated = 0
    tab_rows = _tab_rows(grid, rows)
    for row in tab_rows:
        if row.kind in _DELETABLE and _is_unedited(row.cells):
            deletes.append(row.index)
            continue
        wanted = _wanted(row, outcomes)
        changed = {
            column: text
            for column, text in wanted.items()
            if _canon(column, row.cells[column]) != _canon(column, text)
        }
        updated += bool(changed)
        updates.extend(
            _update_cells(tab_id, row.index, column, [_cell(column, text)])
            for column, text in changed.items()
        )
        final = [changed.get(Col(i), cell) for i, cell in enumerate(row.cells)]
        intended.append(_verified(final))

    present = {row.job.real_job_id for row in tab_rows if row.job is not None}
    new_jobs = [
        job for job in rows if job.real_job_id not in present and job.source_job_ids
    ]
    appends = []
    for offset, job in enumerate(new_jobs):
        filled = _new_row_cells(job)
        cells = [filled[column] for column in Col]
        values = [_cell(column, filled[column]) for column in Col]
        appends.append(_update_cells(tab_id, len(grid) + offset, 0, values))
        intended.append(_verified(cells))

    end_row = len(grid) + len(new_jobs) - len(deletes)
    replaced = deletes and end_row > 1
    return SyncPlan(
        requests=(
            *updates,
            *appends,
            *(_delete_row(tab_id, index) for index in sorted(deletes, reverse=True)),
            *([_replace_rows(tab_id, end_row, len(deletes))] if replaced else []),
            *([_sort_by_fit(tab_id, end_row)] if new_jobs else []),
        ),
        intended=tuple(intended),
        added=len(new_jobs),
        updated=updated,
        removed=len(deletes),
    )


def readback_mismatches(grid: Sequence[Sequence[str]], plan: SyncPlan) -> list[str]:
    """Compare a tab read after a write with what the plan intended.

    Only the columns the sync owns count, so the user may edit their columns or sort
    the tab while a run is in progress.

    Args:
        grid: The tab's rows as read, header first.
        plan: The plan that was written.

    Returns:
        The job IDs of rows that differ, empty when the tab holds what was intended.
    """
    actual = Counter(
        _verified(cells)
        for cells in map(_pad, grid[1:])
        if any(cell.strip() for cell in cells)
    )
    intended = Counter(plan.intended)
    return sorted({row[0] for row in (actual - intended) + (intended - actual)})
