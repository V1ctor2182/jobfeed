"""What one sync run plans, checked by applying the requests to a model of the tab."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from jobfeed.domain.recruiter_sheet import SheetEdit, SheetRow, SheetStatus
from jobfeed.domain.recruiter_sheet_sync import (
    HEADER,
    Col,
    EditOutcomes,
    plan_sync,
    preview_edits,
    readback_mismatches,
    sheet_edits,
)
from tests.support.fake_google_sheet import TAB_ROWS, Grid

TAB = 7


def job(real_id: int, fit: int | None = 85, **changes: Any) -> SheetRow:
    row = SheetRow(
        real_job_id=real_id,
        source_job_ids=(real_id * 10,),
        company=f"Company {real_id}",
        title=f"Role {real_id}",
        location="New York, NY",
        fit=fit,
        verdict="apply",
        why="Python",
        watch_out="Airflow",
        url=f"https://example.com/{real_id}",
        status=SheetStatus.TO_ASK,
        recruiter="",
        note="",
        updated_at="2026-11-06T01:02:03.000000Z",
        status_updated_at=None,
        closed=False,
        applied=False,
        queue_state=None,
        qualifies=True,
        pay=150000,
        funding="Series A, $17M, 2026-03-17",
        funding_source="https://example.com/round",
        funding_pending=False,
    )
    return replace(row, **changes)


def cells(row: SheetRow, *, status: str = "to ask", **changes: str) -> list[str]:
    """The tab's row for a job already in step, trimmed as get_values trims it."""
    values = [
        str(row.source_job_ids[0]),
        row.company,
        row.title,
        row.location,
        "" if row.fit is None else str(row.fit),
        row.why,
        row.watch_out,
        row.url or "",
        status,
        row.recruiter,
        row.note,
        (row.updated_at or "")[:10],
        "",
        "" if row.pay is None else str(row.pay),
        row.funding,
        row.funding_source or "",
    ]
    for name, value in changes.items():
        values[Col[name.upper()]] = value
    return values


def tab(*rows: list[str]) -> list[list[str]]:
    return Grid([list(HEADER), *rows]).values()


def touched_columns(requests: tuple[dict[str, Any], ...]) -> set[tuple[int, int]]:
    return {
        (
            request["updateCells"]["range"]["startRowIndex"],
            column,
        )
        for request in requests
        if "updateCells" in request
        for column in range(
            request["updateCells"]["range"]["startColumnIndex"],
            request["updateCells"]["range"]["endColumnIndex"],
        )
    }


def applied(grid: list[list[str]], requests: tuple[dict[str, Any], ...]) -> Grid:
    model = Grid(grid)
    model.apply(requests)
    return model


def test_nothing_changed_needs_no_write() -> None:
    row = job(1)
    plan = plan_sync(tab(cells(row)), [row], EditOutcomes(), TAB)
    assert plan.requests == ()
    assert sheet_edits(tab(cells(row)), [row]) == []


@pytest.mark.parametrize("shown", ["85", "85.0"])
def test_a_number_read_back_as_text_is_not_rewritten(shown: str) -> None:
    row = job(1)
    grid = tab(cells(row, fit=shown, job_id="10.0"))
    assert plan_sync(grid, [row], EditOutcomes(), TAB).requests == ()


def test_only_changed_cells_are_written_with_the_tabs_id() -> None:
    row = job(1, company="New name")
    plan = plan_sync(tab(cells(job(1))), [row], EditOutcomes(), TAB)
    [request] = plan.requests
    assert request == {
        "updateCells": {
            "range": {
                "sheetId": TAB,
                "startRowIndex": 1,
                "endRowIndex": 2,
                "startColumnIndex": Col.COMPANY,
                "endColumnIndex": Col.COMPANY + 1,
            },
            "rows": [{"values": [{"userEnteredValue": {"stringValue": "New name"}}]}],
            "fields": "userEnteredValue",
        }
    }
    assert plan.updated == 1


def test_a_formula_looking_company_stays_a_string() -> None:
    text = '=HYPERLINK("http://evil","x")'
    plan = plan_sync(tab(cells(job(1))), [job(1, company=text)], EditOutcomes(), TAB)
    [request] = plan.requests
    assert request["updateCells"]["rows"][0]["values"] == [
        {"userEnteredValue": {"stringValue": text}}
    ]


def test_a_blanked_value_clears_with_an_empty_cell() -> None:
    plan = plan_sync(tab(cells(job(1))), [job(1, why="")], EditOutcomes(), TAB)
    [request] = plan.requests
    assert request["updateCells"]["rows"][0]["values"] == [{}]
    assert request["updateCells"]["fields"] == "userEnteredValue"


def test_fit_is_written_as_a_number() -> None:
    plan = plan_sync(tab(cells(job(1))), [job(1, fit=90)], EditOutcomes(), TAB)
    [request] = plan.requests
    assert request["updateCells"]["rows"][0]["values"] == [
        {"userEnteredValue": {"numberValue": 90}}
    ]


def test_updated_is_the_date_only() -> None:
    row = job(1, updated_at="2026-11-07T09:00:00.000000Z")
    plan = plan_sync(tab(cells(job(1))), [row], EditOutcomes(), TAB)
    [request] = plan.requests
    assert request["updateCells"]["rows"][0]["values"] == [
        {"userEnteredValue": {"stringValue": "2026-11-07"}}
    ]


def test_user_edit_is_an_edit_and_the_plan_never_writes_recruiter_or_note() -> None:
    stored = job(1)
    edited = cells(stored, status="asked", recruiter=" Ana ", note="Called her")
    assert sheet_edits(tab(edited), [stored]) == [
        SheetEdit(1, "asked", "Ana", "Called her")
    ]
    after = job(1, status=SheetStatus.ASKED, recruiter="Ana", note="Called her")
    plan = plan_sync(tab(edited), [after], EditOutcomes(frozenset({1})), TAB)
    columns = {column for _, column in touched_columns(plan.requests)}
    assert not columns & {Col.RECRUITER, Col.NOTE, Col.STATUS}


def test_a_merged_jobs_other_source_id_still_matches_and_stays_in_the_cell() -> None:
    row = job(1, source_job_ids=(10, 11))
    grid = tab(cells(row, job_id="11"))
    assert plan_sync(grid, [row], EditOutcomes(), TAB).requests == ()


def test_a_blank_status_with_other_edits_keeps_the_stored_status_and_restores() -> None:
    stored = job(1, status=SheetStatus.ASKED)
    edited = cells(stored, status="", recruiter="Ana")
    assert sheet_edits(tab(edited), [stored]) == [SheetEdit(1, "asked", "Ana", "")]
    after = replace(stored, recruiter="Ana")
    plan = plan_sync(tab(edited), [after], EditOutcomes(frozenset({1})), TAB)
    model = applied(tab(edited), plan.requests)
    assert model.values()[1][Col.STATUS] == "asked"


def test_a_blank_status_alone_is_no_edit_and_is_restored() -> None:
    stored = job(1, status=SheetStatus.WITH_RECRUITER)
    blank = cells(stored, status="")
    assert sheet_edits(tab(blank), [stored]) == []
    plan = plan_sync(tab(blank), [stored], EditOutcomes(), TAB)
    assert applied(tab(blank), plan.requests).values()[1][Col.STATUS] == (
        "with recruiter"
    )


def test_an_unknown_status_is_refused_and_restored() -> None:
    stored = job(1)
    grid = tab(cells(stored, status="maybe"))
    assert sheet_edits(grid, [stored]) == [SheetEdit(1, "maybe", "", "")]
    plan = plan_sync(grid, [stored], EditOutcomes(refused={1: "Bad status."}), TAB)
    row = applied(grid, plan.requests).values()[1]
    assert row[Col.STATUS] == "to ask"
    assert row[Col.SYNC_NOTE] == "Refused: Bad status."


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        (
            "A row cannot go from with_recruiter to to_ask.",
            "A row cannot go from with recruiter to to ask.",
        ),
        (
            "A row cannot go from apply_myself to with_recruiter.",
            "A row cannot go from apply myself to with recruiter.",
        ),
        (
            "Status must be one of to_ask, asked, with_recruiter, passed, "
            "apply_myself, not 'maybe'.",
            "Status must be one of to ask, asked, with recruiter, passed, "
            "apply myself, not 'maybe'.",
        ),
    ],
)
def test_a_refusal_uses_the_sheets_words(message: str, shown: str) -> None:
    stored = job(1)
    grid = tab(cells(stored))
    plan = plan_sync(grid, [stored], EditOutcomes(refused={1: message}), TAB)
    assert applied(grid, plan.requests).values()[1][Col.SYNC_NOTE] == (
        f"Refused: {shown}"
    )


def test_a_refused_note_stays_until_a_later_applied_edit_clears_it() -> None:
    stored = job(1, status=SheetStatus.WITH_RECRUITER)
    grid = tab(cells(stored, status="to ask"))
    refused = EditOutcomes(refused={1: "A row cannot go there."})
    plan = plan_sync(grid, [stored], refused, TAB)
    grid = applied(grid, plan.requests).values()
    assert grid[1][Col.STATUS] == "with recruiter"
    assert grid[1][Col.SYNC_NOTE] == "Refused: A row cannot go there."

    assert plan_sync(grid, [stored], EditOutcomes(), TAB).requests == ()

    later = replace(stored, note="Fine")
    plan = plan_sync(grid, [later], EditOutcomes(frozenset({1})), TAB)
    after = applied(grid, plan.requests).values()[1]
    assert after[Col.SYNC_NOTE] == ""


@pytest.mark.parametrize(
    ("changes", "note"),
    [
        ({"closed": True}, "Posting closed"),
        ({"applied": True}, "Applied"),
        ({"closed": True, "applied": True}, "Posting closed · Applied"),
        ({"qualifies": False}, "No longer qualifies"),
        ({"qualifies": False, "closed": True}, "Posting closed"),
    ],
)
def test_facts_are_recomputed_each_run(changes: dict[str, bool], note: str) -> None:
    row = job(1, **changes)
    grid = tab(cells(row))
    plan = plan_sync(grid, [row], EditOutcomes(), TAB)
    grid = applied(grid, plan.requests).values()
    assert grid[1][Col.SYNC_NOTE] == note
    assert plan_sync(grid, [job(1)], EditOutcomes(), TAB).updated == 1


def test_facts_and_a_refusal_join_in_order() -> None:
    row = job(1, closed=True)
    grid = tab(cells(row, status="nope"))
    plan = plan_sync(grid, [row], EditOutcomes(refused={1: "Bad."}), TAB)
    assert applied(grid, plan.requests).values()[1][Col.SYNC_NOTE] == (
        "Posting closed · Refused: Bad."
    )


def test_new_rows_go_at_the_end_with_stored_user_values_and_then_sort() -> None:
    low, high = job(1, fit=80), job(2, fit=95, status=SheetStatus.ASKED, note="Hi")
    grid = tab(cells(job(3, fit=90)))
    plan = plan_sync(grid, [high, job(3, fit=90), low], EditOutcomes(), TAB)
    model = applied(grid, plan.requests).values()
    assert [row[Col.JOB_ID] for row in model[1:]] == ["20", "30", "10"]
    assert model[1][Col.STATUS] == "asked"
    assert model[1][Col.NOTE] == "Hi"
    assert model[3][Col.STATUS] == "to ask"
    assert (plan.added, plan.removed) == (2, 0)
    assert plan.requests[-1] == {
        "sortRange": {
            "range": {
                "sheetId": TAB,
                "startRowIndex": 1,
                "endRowIndex": 4,
                "startColumnIndex": 0,
                "endColumnIndex": 16,
            },
            "sortSpecs": [{"dimensionIndex": 4, "sortOrder": "DESCENDING"}],
        }
    }


def test_no_sort_when_nothing_was_added() -> None:
    row = job(1, fit=70)
    plan = plan_sync(tab(cells(row)), [replace(row, fit=99)], EditOutcomes(), TAB)
    assert not any("sortRange" in request for request in plan.requests)


def test_a_row_with_no_source_posting_cannot_be_added() -> None:
    plan = plan_sync(tab(), [job(1, source_job_ids=())], EditOutcomes(), TAB)
    assert plan.requests == ()


def test_an_unedited_departed_row_is_deleted() -> None:
    grid = tab(cells(job(1)), cells(job(2)))
    plan = plan_sync(grid, [job(2)], EditOutcomes(), TAB)
    assert [row[Col.JOB_ID] for row in applied(grid, plan.requests).values()[1:]] == [
        "20"
    ]
    assert plan.removed == 1


@pytest.mark.parametrize(
    "edit", [{"status": "asked"}, {"recruiter": "Ana"}, {"note": "x"}, {"status": ""}]
)
def test_an_edited_departed_row_is_kept_with_a_note(edit: dict[str, str]) -> None:
    grid = tab(cells(job(1), **edit))
    plan = plan_sync(grid, [], EditOutcomes(), TAB)
    row = applied(grid, plan.requests).values()[1]
    assert row[Col.JOB_ID] == "10"
    assert "left the list" in row[Col.SYNC_NOTE]
    assert plan.removed == 0
    assert (
        plan_sync(
            applied(grid, plan.requests).values(), [], EditOutcomes(), TAB
        ).requests
        == ()
    )


def test_duplicate_rows_keep_the_first_and_note_or_delete_the_rest() -> None:
    merged = job(1, source_job_ids=(10, 11))
    grid = tab(
        cells(merged),
        cells(merged, job_id="11"),
        cells(merged, job_id="10", note="Keep me"),
    )
    plan = plan_sync(grid, [merged], EditOutcomes(), TAB)
    rows = applied(grid, plan.requests).values()[1:]
    assert [row[Col.NOTE] for row in rows] == ["", "Keep me"]
    assert rows[1][Col.SYNC_NOTE].startswith("Duplicate")
    assert sheet_edits(grid, [merged]) == []


def test_blank_rows_are_skipped_and_an_unparsable_id_gets_a_note() -> None:
    grid = tab(
        ["abc", "Hand typed"],
        [],
        ["12.5", "Half"],
        ["", "No id"],
        cells(job(1)),
    )
    plan = plan_sync(grid, [job(1)], EditOutcomes(), TAB)
    rows = applied(grid, plan.requests).values()
    assert plan.removed == 0
    assert [
        row[Col.SYNC_NOTE] if len(row) > Col.SYNC_NOTE else "" for row in rows[1:]
    ] == [
        "Job ID not recognized, so this row was not synced",
        "",
        "Job ID not recognized, so this row was not synced",
        "Job ID not recognized, so this row was not synced",
        "",
    ]


def test_deletes_updates_and_appends_combine_with_correct_coordinates() -> None:
    kept_a = job(1, fit=90)
    gone_a = job(2, fit=88)
    kept_b = job(3, fit=70)
    gone_b = job(4, fit=60)
    kept_c = job(5, fit=50)
    new_a, new_b = job(6, fit=95), job(7, fit=40)
    grid = tab(
        cells(kept_a, status="asked", recruiter="Ana"),
        cells(gone_a),
        cells(kept_b, note="Mine"),
        cells(gone_b),
        [],
        cells(kept_c, recruiter="Ben"),
    )
    rows = [
        new_a,
        replace(kept_a, company="Changed", status=SheetStatus.ASKED, recruiter="Ana"),
        replace(kept_b, note="Mine", why="Better"),
        replace(kept_c, recruiter="Ben", closed=True),
        new_b,
    ]
    plan = plan_sync(grid, rows, EditOutcomes(), TAB)
    assert (plan.added, plan.removed) == (2, 2)
    values = applied(grid, plan.requests).values()
    by_id = {row[Col.JOB_ID]: row for row in values[1:] if row}
    assert list(by_id) == ["60", "10", "30", "50", "70"]
    assert by_id["10"][Col.COMPANY] == "Changed"
    assert by_id["10"][Col.RECRUITER] == "Ana"
    assert by_id["30"][Col.WHY] == "Better"
    assert by_id["30"][Col.NOTE] == "Mine"
    assert by_id["50"][Col.SYNC_NOTE] == "Posting closed"
    assert by_id["50"][Col.RECRUITER] == "Ben"
    assert "20" not in by_id
    assert "40" not in by_id
    assert readback_mismatches(values, plan) == []
    assert plan_sync(values, rows, EditOutcomes(), TAB).requests == ()


def request_kinds(requests: tuple[dict[str, Any], ...]) -> list[str]:
    return [next(iter(request)) for request in requests]


def test_deleted_rows_are_replaced_below_the_data_so_the_tab_keeps_its_size() -> None:
    grid = tab(cells(job(1)), cells(job(2)), cells(job(3)))
    plan = plan_sync(grid, [job(2)], EditOutcomes(), TAB)
    assert plan.requests[-1] == {
        "insertDimension": {
            "range": {
                "sheetId": TAB,
                "dimension": "ROWS",
                "startIndex": 2,
                "endIndex": 4,
            },
            "inheritFromBefore": True,
        }
    }
    model = applied(grid, plan.requests)
    assert len(model.cells) == TAB_ROWS
    assert [row[Col.JOB_ID] for row in model.values()[1:]] == ["20"]


def test_replacing_rows_holds_with_updates_appends_and_the_sort() -> None:
    stay_a, stay_b = job(1, fit=90), job(2, fit=70)
    gone_a, gone_b = job(3, fit=80), job(4, fit=60)
    new_a, new_b = job(5, fit=95), job(6, fit=50)
    grid = tab(
        cells(stay_a, recruiter="Ana"),
        cells(gone_a),
        cells(stay_b, note="Mine"),
        [],
        cells(gone_b),
    )
    rows = [
        new_a,
        replace(stay_a, company="Changed", recruiter="Ana"),
        replace(stay_b, note="Mine"),
        new_b,
    ]
    plan = plan_sync(grid, rows, EditOutcomes(), TAB)
    kinds = request_kinds(plan.requests)
    assert kinds == [
        "updateCells",
        "updateCells",
        "updateCells",
        "deleteDimension",
        "deleteDimension",
        "insertDimension",
        "sortRange",
    ]
    end_row = 6
    insert = plan.requests[kinds.index("insertDimension")]["insertDimension"]
    assert (insert["range"]["startIndex"], insert["range"]["endIndex"]) == (
        end_row,
        end_row + 2,
    )
    sort = plan.requests[kinds.index("sortRange")]["sortRange"]
    assert sort["range"]["endRowIndex"] == end_row

    model = applied(grid, plan.requests)
    assert len(model.cells) == TAB_ROWS
    values = model.values()
    assert [row[Col.JOB_ID] for row in values[1:]] == ["50", "10", "20", "60"]
    assert values[2][Col.COMPANY] == "Changed"
    assert values[2][Col.RECRUITER] == "Ana"
    assert values[3][Col.NOTE] == "Mine"
    assert readback_mismatches(values, plan) == []
    assert plan_sync(values, rows, EditOutcomes(), TAB).requests == ()


def test_a_blank_row_above_the_new_rows_is_still_below_the_header() -> None:
    grid = tab(cells(job(1)), [], cells(job(2)))
    plan = plan_sync(grid, [job(1)], EditOutcomes(), TAB)
    model = applied(grid, plan.requests)
    assert len(model.cells) == TAB_ROWS
    assert [row[Col.JOB_ID] for row in model.values()[1:]] == ["10"]


def test_no_row_is_added_when_only_the_header_would_remain() -> None:
    grid = tab(cells(job(1)), cells(job(2)))
    plan = plan_sync(grid, [], EditOutcomes(), TAB)
    assert request_kinds(plan.requests) == ["deleteDimension", "deleteDimension"]
    assert len(applied(grid, plan.requests).cells) == TAB_ROWS - 2


def test_no_row_is_added_when_nothing_was_deleted() -> None:
    plan = plan_sync(tab(cells(job(1))), [job(1, why="New")], EditOutcomes(), TAB)
    assert "insertDimension" not in request_kinds(plan.requests)


def test_a_full_tab_refuses_the_batch_rather_than_writing_part_of_it() -> None:
    grid = Grid([list(HEADER), cells(job(1))], rows=2)
    plan = plan_sync(grid.values(), [job(1), job(2, fit=99)], EditOutcomes(), TAB)
    with pytest.raises(ValueError, match="grid limits"):
        grid.apply(plan.requests)


def test_readback_compares_sync_columns_only_and_names_the_job() -> None:
    row = job(1)
    grid = tab(cells(row, status="asked", recruiter="Ana"))
    plan = plan_sync(
        grid,
        [replace(row, status=SheetStatus.ASKED, recruiter="Ana")],
        EditOutcomes(),
        TAB,
    )
    user_changed = tab(cells(row, status="passed", recruiter="Other", note="New"))
    assert readback_mismatches(user_changed, plan) == []
    tampered = tab(cells(row, company="Tampered"))
    assert readback_mismatches(tampered, plan) == ["10"]
    assert readback_mismatches(tab(), plan) == ["10"]


def test_a_new_row_writes_pay_as_a_number_and_funding_as_text() -> None:
    plan = plan_sync(tab(), [job(1)], EditOutcomes(), TAB)
    [write] = [r["updateCells"] for r in plan.requests if "updateCells" in r]
    assert (write["range"]["startColumnIndex"], write["range"]["endColumnIndex"]) == (
        0,
        len(HEADER),
    )
    values = write["rows"][0]["values"]
    assert values[Col.PAY] == {"userEnteredValue": {"numberValue": 150000}}
    assert values[Col.FUNDING] == {
        "userEnteredValue": {"stringValue": "Series A, $17M, 2026-03-17"}
    }
    assert values[Col.FUNDING_SOURCE] == {
        "userEnteredValue": {"stringValue": "https://example.com/round"}
    }


def test_a_row_written_before_the_new_columns_gains_them_and_keeps_the_rest() -> None:
    row = job(1)
    grid = tab(cells(row, status="asked", recruiter="Ana", pay="", funding=""))
    grid[1] = grid[1][: Col.FUNDING_SOURCE]
    plan = plan_sync(
        grid,
        [replace(row, status=SheetStatus.ASKED, recruiter="Ana")],
        EditOutcomes(),
        TAB,
    )
    assert touched_columns(plan.requests) == {
        (1, Col.PAY),
        (1, Col.FUNDING),
        (1, Col.FUNDING_SOURCE),
    }
    after = applied(grid, plan.requests).values()[1]
    assert after[Col.RECRUITER] == "Ana"
    assert after[Col.PAY :] == ["150000", row.funding, row.funding_source]
    assert readback_mismatches(applied(grid, plan.requests).values(), plan) == []


def test_pay_compares_as_a_number_so_a_formatted_cell_is_left_alone() -> None:
    row = job(1)
    grid = tab(cells(row, pay="150,000"))
    assert plan_sync(grid, [row], EditOutcomes(), TAB).requests == ()


@pytest.mark.parametrize(
    "tampered",
    [
        {"pay": "90000"},
        {"funding": "Series Z"},
        {"funding_source": "https://other.example"},
    ],
)
def test_readback_covers_the_new_columns(tampered: dict[str, str]) -> None:
    row = job(1)
    plan = plan_sync(tab(), [row], EditOutcomes(), TAB)
    assert readback_mismatches(tab(cells(row)), plan) == []
    assert readback_mismatches(tab(cells(row, **tampered)), plan) == ["10"]


def test_a_pending_row_says_so_and_loses_the_note_once_checked() -> None:
    pending = job(1, pay=150000, funding="", funding_source=None, funding_pending=True)
    plan = plan_sync(tab(), [pending], EditOutcomes(), TAB)
    grid = applied(tab(), plan.requests).values()
    assert grid[1][Col.SYNC_NOTE] == "Funding check pending"

    checked = job(1)
    plan = plan_sync(grid, [checked], EditOutcomes(), TAB)
    after = applied(grid, plan.requests).values()[1]
    assert after[Col.SYNC_NOTE] == ""
    assert after[Col.FUNDING] == checked.funding


def test_preview_works_out_outcomes_without_a_database() -> None:
    stored = job(1, status=SheetStatus.WITH_RECRUITER)
    other = job(2)
    edits = [
        SheetEdit(1, "to_ask", "", ""),
        SheetEdit(2, "asked", "Ana", ""),
    ]
    rows, outcomes = preview_edits([stored, other], edits)
    assert outcomes.applied == frozenset({2})
    assert "cannot go from" in outcomes.refused[1]
    assert rows[0].status is SheetStatus.WITH_RECRUITER
    assert (rows[1].status, rows[1].recruiter) == (SheetStatus.ASKED, "Ana")
