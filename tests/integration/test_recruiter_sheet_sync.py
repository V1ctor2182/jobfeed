"""Whole sync runs against a real Jobfeed database and a fake ``claude``."""

from __future__ import annotations

import fcntl
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jobfeed.config import load_settings
from jobfeed.domain.recruiter_sheet import (
    SheetEdit,
    SheetPolicy,
    SheetStatus,
    held_source_ids,
)
from jobfeed.domain.recruiter_sheet_sync import HEADER, Col
from jobfeed.recruiter_sheet import apply_edits, read_sheet
from jobfeed.recruiter_sheet_sync import (
    LOCK_NAME,
    LOOKUP_SECONDS,
    MAX_LOOKUPS,
    SheetSyncError,
    _look_up_funding,
    _Tally,
    failure_reason,
    main,
    sync_once,
)
from tests.integration.test_recruiter_sheet_sqlite import PAID, Fixture, stage_b
from tests.support.fake_claude import FakeClaude, install_fake_claude
from tests.support.sqlite_jobs_evaluations import open_sqlite_store

READ_TOOL = "mcp__claude_ai_Google_Sheets__get_values"
WRITE_TOOL = "mcp__claude_ai_Google_Sheets__update_spreadsheet"
POLICY = SheetPolicy()
TAB = 42
FAILED = 1
RECENT = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()


@pytest.fixture
async def sheet(tmp_path: Path) -> AsyncIterator[Fixture]:
    path = tmp_path / "sheet.db"
    lifecycle, store = await open_sqlite_store(path)
    try:
        yield Fixture(path, store, lifecycle)
    finally:
        await lifecycle.close()


def config(tmp_path: Path, sheet_id: str = "sheet-1") -> str:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[db]\npath = "{tmp_path / "sheet.db"}"\n\n'
        f'[recruiter_sheet]\ngoogle_sheet_id = "{sheet_id}"\n'
        f"google_sheet_tab_id = {TAB}\n"
    )
    return str(path)


def tab_with(*rows: Sequence[str]) -> list[list[str]]:
    return [list(HEADER), *(list(row) for row in rows)]


def synced_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """A fake holding only a header, brought in step by one real run."""
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path)]) == 0
    return fake


def row_for(fake: FakeClaude, source_id: int) -> list[str]:
    for row in fake.values()[1:]:
        if row and row[Col.JOB_ID] == str(source_id):
            return [*row, *[""] * (len(HEADER) - len(row))]
    raise AssertionError(f"no row for {source_id}")


def edit_cell(fake: FakeClaude, source_id: int, **changes: str) -> None:
    values = fake.values()
    for index, raw in enumerate(values):
        if raw and raw[0] == str(source_id):
            row = [*raw, *[""] * (len(HEADER) - len(raw))]
            for name, value in changes.items():
                row[Col[name.upper()]] = value
            values[index] = row
    fake.set_values(values)


def write_requests(fake: FakeClaude) -> list[dict[str, Any]]:
    return [request for write in fake.writes() for request in write["requests"]]


def touches_user_columns(request: dict[str, Any]) -> bool:
    area = request.get("updateCells", {}).get("range")
    if area is None:
        return False
    return (
        area["startColumnIndex"] <= Col.NOTE and area["endColumnIndex"] > Col.RECRUITER
    )


async def test_new_jobs_are_added_sorted_with_fit_a_number_and_a_tab_id(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, low = await sheet.job("low", evaluation=stage_b(score=80))
    _, high = await sheet.job("high", evaluation=stage_b(score=95))
    fake = synced_tab(tmp_path, monkeypatch)
    assert [row[Col.JOB_ID] for row in fake.values()[1:]] == [str(high), str(low)]
    assert row_for(fake, high)[Col.STATUS] == "to ask"
    assert row_for(fake, high)[Col.FIT] == "95"
    assert row_for(fake, high)[Col.UPDATED] == "2026-11-06"
    first, *_ = write_requests(fake)
    assert first["updateCells"]["range"]["sheetId"] == TAB
    assert write_requests(fake)[-1]["sortRange"]["range"]["sheetId"] == TAB


async def test_nothing_changed_makes_no_write_call(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    fake = synced_tab(tmp_path, monkeypatch)
    calls = len(fake.calls())
    writes = len(fake.writes())
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.calls()) == calls + 1
    assert fake.calls()[-1]["tool"] == READ_TOOL
    assert len(fake.writes()) == writes


async def test_a_user_edit_reaches_the_database_and_never_writes_their_columns(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_id, source = await sheet.job("a")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="asked", recruiter="Ana", note="Called on Monday")
    writes_before = len(write_requests(fake))
    assert main(["--config", config(tmp_path)]) == 0
    [row] = read_sheet(sheet.path, POLICY)
    assert row.real_job_id == real_id
    assert row.status is SheetStatus.ASKED
    assert (row.recruiter, row.note) == ("Ana", "Called on Monday")
    assert not any(
        touches_user_columns(request)
        for request in write_requests(fake)[writes_before:]
    )
    assert row_for(fake, source)[Col.RECRUITER] == "Ana"


async def test_a_refused_transition_restores_the_cell_and_keeps_the_note_until_applied(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_id, source = await sheet.job("a")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="with recruiter")
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.STATUS] == "with recruiter"

    edit_cell(fake, source, status="to ask")
    assert main(["--config", config(tmp_path)]) == 0
    assert read_sheet(sheet.path, POLICY)[0].status is SheetStatus.WITH_RECRUITER
    row = row_for(fake, source)
    assert row[Col.STATUS] == "with recruiter"
    assert row[Col.SYNC_NOTE].startswith("Refused: A row cannot go from")

    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE].startswith("Refused:")

    edit_cell(fake, source, note="Back in touch")
    assert main(["--config", config(tmp_path)]) == 0
    row = row_for(fake, source)
    assert row[Col.SYNC_NOTE] == ""
    assert read_sheet(sheet.path, POLICY)[0].note == "Back in touch"
    assert read_sheet(sheet.path, POLICY)[0].real_job_id == real_id


async def test_a_departed_row_is_deleted_unless_the_user_wrote_on_it(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, gone_source = await sheet.job("gone")
    kept, kept_source = await sheet.job("kept")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, kept_source, recruiter="Ana")
    for real_id in (gone, kept):
        sheet.sql(
            "UPDATE real_job_evaluations SET stage_b_verdict='consider' "
            "WHERE real_job_id=?",
            real_id,
        )
    assert main(["--config", config(tmp_path)]) == 0
    ids = [row[Col.JOB_ID] for row in fake.values()[1:] if row]
    assert ids == [str(kept_source)]
    row = row_for(fake, kept_source)
    assert row[Col.RECRUITER] == "Ana"
    assert "left the list" in row[Col.SYNC_NOTE]
    assert gone_source != kept_source


async def test_a_deleted_row_leaves_the_tab_the_same_size(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, _ = await sheet.job("gone")
    _, kept_source = await sheet.job("kept")
    fake = synced_tab(tmp_path, monkeypatch)
    size = fake.row_count()
    sheet.sql(
        "UPDATE real_job_evaluations SET stage_b_verdict='consider' "
        "WHERE real_job_id=?",
        gone,
    )
    await sheet.job("new")
    assert main(["--config", config(tmp_path)]) == 0
    assert fake.row_count() == size
    rows_on_tab = [row for row in fake.values()[1:] if row]
    assert len(rows_on_tab) == len(read_sheet(sheet.path, POLICY))
    assert row_for(fake, kept_source)[Col.STATUS] == "to ask"


async def test_a_refusal_from_jobfeed_reads_in_the_sheets_words(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="with recruiter")
    assert main(["--config", config(tmp_path)]) == 0
    edit_cell(fake, source, status="to ask")
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE] == (
        "Refused: A row cannot go from with recruiter to to ask."
    )
    edit_cell(fake, source, status="maybe")
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE] == (
        "Refused: Status must be one of to ask, asked, with recruiter, passed, "
        "apply myself, not 'maybe'."
    )


async def test_a_formula_looking_company_is_written_as_a_string(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a")
    text = '=HYPERLINK("http://evil","x")'
    sheet.sql("UPDATE jobs SET company=? WHERE id=?", text, source)
    fake = synced_tab(tmp_path, monkeypatch)
    assert row_for(fake, source)[Col.COMPANY] == text
    [first, *_] = write_requests(fake)
    assert first["updateCells"]["rows"][0]["values"][Col.COMPANY] == {
        "userEnteredValue": {"stringValue": text}
    }
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.writes()) == 1


async def test_a_wrong_header_fails_and_writes_nothing(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job", "Company"]])
    assert main(["--config", config(tmp_path)]) == FAILED
    assert fake.writes() == []
    assert "unexpected header" in capsys.readouterr().err
    assert read_sheet(sheet.path, POLICY)[0].status is SheetStatus.TO_ASK


async def test_read_back_mismatch_fails_naming_the_job_and_no_posting_text(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, source = await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.behave(WRITE_TOOL, "tamper")
    assert main(["--config", config(tmp_path)]) == FAILED
    err = capsys.readouterr().err.strip()
    assert str(source) in err
    assert "does not hold what was written" in err
    assert "Tampered" not in err
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize(
    "behavior", ["tool_error", "no_tool_result", "nonzero", "not_json"]
)
async def test_a_failed_read_writes_nothing_and_saves_no_edit(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    behavior: str,
) -> None:
    _, source = await sheet.job("a")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="asked")
    writes = len(fake.writes())
    capsys.readouterr()
    fake.behave(READ_TOOL, behavior)
    assert main(["--config", config(tmp_path)]) == FAILED
    assert len(fake.writes()) == writes
    assert read_sheet(sheet.path, POLICY)[0].status is SheetStatus.TO_ASK
    captured = capsys.readouterr()
    assert captured.err.startswith("recruiter sheet sync failed: ")
    assert len(captured.err.strip().splitlines()) == 1
    assert "cost_usd=" in captured.out


async def test_a_failed_write_fails_the_run_and_repeats_nothing(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.behave(WRITE_TOOL, "tool_error")
    assert main(["--config", config(tmp_path)]) == FAILED
    assert fake.writes() == []
    write_calls = [call for call in fake.calls() if call["tool"] == WRITE_TOOL]
    assert len(write_calls) == 1


async def test_an_empty_sheet_id_does_nothing_and_says_so(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path, sheet_id="")]) == 0
    assert fake.calls() == []
    assert "no google_sheet_id" in capsys.readouterr().out


async def test_a_second_run_waits_for_the_first_and_then_syncs(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    codes: list[int] = []
    second = threading.Thread(
        target=lambda: codes.append(main(["--config", config(tmp_path)]))
    )
    with (tmp_path / LOCK_NAME).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        second.start()
        second.join(timeout=0.5)
        assert second.is_alive()
        assert fake.calls() == []
    second.join(timeout=30)
    assert codes == [0]
    assert row_for(fake, source)[Col.JOB_ID] == str(source)


async def test_sync_once_says_whether_it_synced(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert sync_once(load_settings(Path(config(tmp_path))))
    assert not sync_once(load_settings(Path(config(tmp_path, sheet_id=""))))


async def test_sync_once_raises_a_failure_with_a_one_line_reason(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    install_fake_claude(tmp_path, monkeypatch, [["Wrong", "Header"]])
    with pytest.raises(SheetSyncError) as failed:
        sync_once(load_settings(Path(config(tmp_path))))
    assert failure_reason(failed.value) == (
        "the Jobs tab has an unexpected header, so nothing changed"
    )


async def test_a_dry_run_reports_and_saves_and_writes_nothing(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, source = await sheet.job("a")
    await sheet.job("b")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="asked")
    await sheet.job("c")
    writes = len(fake.writes())
    capsys.readouterr()
    assert main(["--config", config(tmp_path), "--dry-run"]) == 0
    assert len(fake.writes()) == writes
    assert read_sheet(sheet.path, POLICY)[0].status is SheetStatus.TO_ASK
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "edits applied=1" in out
    assert "added=1" in out


async def test_the_log_counts_edits_rows_and_cost_with_no_posting_text(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, source = await sheet.job("a")
    sheet.sql("UPDATE jobs SET company='Secret Co' WHERE id=?", source)
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="asked")
    capsys.readouterr()
    assert main(["--config", config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "edits applied=1 refused=0 rows added=0 updated=1 removed=0" in out
    assert "cost_usd=0.0150" in out
    assert "Secret Co" not in out


async def test_an_edit_that_arrived_through_the_api_gets_a_new_row_with_its_values(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_id, source = await sheet.job("a")
    apply_edits(sheet.path, POLICY, [SheetEdit(real_id, "asked", "Ana", "From API")])
    fake = synced_tab(tmp_path, monkeypatch)
    row = row_for(fake, source)
    assert row[Col.STATUS] == "asked"
    assert (row[Col.RECRUITER], row[Col.NOTE]) == ("Ana", "From API")
    assert main(["--config", config(tmp_path)]) == 0
    stored = read_sheet(sheet.path, POLICY)[0]
    assert (stored.recruiter, stored.note) == ("Ana", "From API")


def funding_rows(sheet: Fixture) -> dict[str, int]:
    with closing(sqlite3.connect(sheet.path)) as db:
        return dict(db.execute("SELECT company_key,found FROM company_funding"))


async def test_a_run_stores_each_lookup_and_the_sheet_shows_it(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="Funded")
    await sheet.job("b", company="Unfunded")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Unfunded", answer={"found": False})
    assert main(["--config", config(tmp_path)]) == 0
    assert funding_rows(sheet) == {"funded": 1, "unfunded": 0}
    assert [row.company for row in read_sheet(sheet.path, POLICY)] == ["Funded"]
    assert [row[Col.COMPANY] for row in fake.values()[1:]] == ["Funded"]
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.funding_calls()) == len(funding_rows(sheet))


async def test_a_run_makes_at_most_ten_lookups_best_fit_first_and_the_next_goes_on(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    companies = MAX_LOOKUPS + 2
    for index in range(companies):
        await sheet.job(
            f"job-{index}",
            company=f"Company {index}",
            evaluation=stage_b(score=80 + index),
        )
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path)]) == 0
    called = [call["company"] for call in fake.funding_calls()]
    assert called == [f"Company {companies - 1 - i}" for i in range(MAX_LOOKUPS)]
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.funding_calls()) == companies


async def test_lookups_stop_starting_at_the_time_limit(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        await sheet.job(f"job-{index}", company=f"Company {index}")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    settings = load_settings(Path(config(tmp_path)))
    times: Iterator[float] = iter([0.0, 0.0, LOOKUP_SECONDS - 1.0, LOOKUP_SECONDS])
    tally = _Tally()
    _look_up_funding(settings, tally, clock=lambda: next(times))
    assert tally.lookups == len(fake.funding_calls()) == 2  # noqa: PLR2004


async def test_every_lookup_failing_finishes_the_sheet_and_then_fails_the_run(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a", company="Broken", jd_text=f"Secret text. {PAID}")
    await sheet.job("b", company="Also broken", jd_text=f"Secret text. {PAID}")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=3)
    assert main(["--config", config(tmp_path)]) == FAILED
    assert funding_rows(sheet) == {}
    assert {row[Col.COMPANY] for row in fake.values()[1:]} == {"Broken", "Also broken"}
    captured = capsys.readouterr()
    [failure] = captured.err.splitlines()
    assert "all 2 funding lookups failed" in failure
    assert "claude exited 3" in failure
    assert "errors=2" in captured.out
    assert "Secret" not in captured.out + captured.err


async def test_one_lookup_failing_is_only_logged_and_keeps_the_stale_state(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a", company="Broken")
    await sheet.job("b", company="Fine")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Broken", answer={"round": ""})
    assert main(["--config", config(tmp_path)]) == 0
    assert funding_rows(sheet) == {"fine": 1}
    output = capsys.readouterr().out
    assert "funding lookup failed for broken" in output
    assert "funding lookups=2 found=1 errors=1 funding_cost_usd=0.0500" in output


async def test_a_single_failed_lookup_only_logs_and_the_run_succeeds(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a", company="Broken")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Broken", exit=3)
    assert main(["--config", config(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "funding lookup failed for broken: claude exited 3" in captured.out
    assert [row[Col.COMPANY] for row in fake.values()[1:]] == ["Broken"]


async def test_a_failed_lookup_is_retried_on_the_next_run(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="Flaky")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Flaky", exit=3)
    assert main(["--config", config(tmp_path)]) == 0
    fake.funding("Flaky")
    assert main(["--config", config(tmp_path)]) == 0
    assert funding_rows(sheet) == {"flaky": 1}


async def test_a_dry_run_makes_no_lookup_and_stores_nothing(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path), "--dry-run"]) == 0
    assert fake.funding_calls() == []
    assert fake.writes() == []
    assert funding_rows(sheet) == {}


async def test_a_wrong_header_spends_no_lookup(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sheet.job("a", company="Fine")
    fake = install_fake_claude(tmp_path, monkeypatch, [["Wrong"]])
    assert main(["--config", config(tmp_path)]) == FAILED
    assert fake.funding_calls() == []
    assert funding_rows(sheet) == {}
    assert "unexpected header" in capsys.readouterr().err


async def test_a_failed_first_read_spends_no_lookup(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="Fine")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.behave(READ_TOOL, "nonzero")
    assert main(["--config", config(tmp_path)]) == FAILED
    assert fake.funding_calls() == []


async def test_a_run_writes_and_verifies_pay_and_funding_in_columns_n_to_p(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a", company="Funded")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding(
        "Funded",
        answer={"announced_on": RECENT, "source_url": "https://news.example/f"},
    )
    assert main(["--config", config(tmp_path)]) == 0
    assert fake.values()[0][Col.PAY :] == ["Pay", "Funding", "Funding source"]
    row = row_for(fake, source)
    assert row[Col.PAY :] == [
        "175000",
        f"Series A, $17M, {RECENT}",
        "https://news.example/f",
    ]
    fake.behave(WRITE_TOOL, "tamper")
    edit_cell(fake, source, pay="1")
    assert main(["--config", config(tmp_path)]) == FAILED
    assert main(["--config", config(tmp_path)]) == 0


async def test_a_tab_with_the_old_header_fails_with_nothing_written(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a")
    fake = install_fake_claude(tmp_path, monkeypatch, [list(HEADER[: Col.PAY])])
    assert main(["--config", config(tmp_path)]) == FAILED
    assert fake.writes() == []


async def test_a_pending_row_is_on_the_tab_with_a_note_when_its_lookup_failed(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a", company="Broken")
    await sheet.job("b", company="Fine")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Broken", exit=1)
    assert main(["--config", config(tmp_path)]) == 0
    row = row_for(fake, source)
    assert row[Col.SYNC_NOTE] == "Funding check pending"
    assert row[Col.PAY :] == ["175000", "", ""]
    fake.funding("Broken")
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE] == ""


async def test_a_row_that_stops_qualifying_after_its_check_is_removed_unless_edited(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plain = await sheet.job("a", company="Plain")
    _, edited = await sheet.job("b", company="Edited")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=1)
    assert main(["--config", config(tmp_path)]) == FAILED
    edit_cell(fake, edited, recruiter="Sam")
    assert main(["--config", config(tmp_path)]) == FAILED
    fake.funding("*", answer={"found": False})
    assert main(["--config", config(tmp_path)]) == 0
    assert [row[Col.JOB_ID] for row in fake.values()[1:]] == [str(edited)]
    row = row_for(fake, edited)
    assert row[Col.RECRUITER] == "Sam"
    assert row[Col.FUNDING] == "None found"
    assert row[Col.SYNC_NOTE] == "No longer qualifies"
    assert plain not in held_source_ids(read_sheet(sheet.path, POLICY))


async def test_a_job_edited_in_the_run_its_check_comes_back_negative_stays_held(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a", company="Unlucky")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=1)
    main(["--config", config(tmp_path)])
    edit_cell(fake, source, recruiter="Sam")
    fake.funding("*", answer={"found": False})
    assert main(["--config", config(tmp_path)]) == 0
    row = row_for(fake, source)
    assert row[Col.RECRUITER] == "Sam"
    assert row[Col.SYNC_NOTE] == "No longer qualifies"
    [stored] = read_sheet(sheet.path, POLICY)
    assert (stored.recruiter, stored.qualifies) == ("Sam", False)
    assert source in held_source_ids([stored])


async def test_a_run_with_a_lookup_reads_the_tab_again_before_it_plans(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="New")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path)]) == 0
    assert [call["tool"] for call in fake.calls()] == [
        READ_TOOL,
        READ_TOOL,
        WRITE_TOOL,
        READ_TOOL,
    ]


async def test_a_run_with_no_lookup_reads_the_tab_once_before_it_plans(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a", company="Known")
    sheet.sql(
        "INSERT INTO company_funding VALUES('known','Known',1,'Seed',NULL,?,"
        "'https://news.example/known',?)",
        RECENT,
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    fake = install_fake_claude(
        tmp_path, monkeypatch, tab_with([str(source), "Stale company"])
    )
    assert main(["--config", config(tmp_path)]) == 0
    assert fake.funding_calls() == []
    assert [call["tool"] for call in fake.calls()] == [
        READ_TOOL,
        WRITE_TOOL,
        READ_TOOL,
    ]


async def test_an_edit_and_a_sort_during_the_lookups_land_on_the_right_rows(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first = await sheet.job("a", company="First", evaluation=stage_b(score=90))
    _, second = await sheet.job("b", company="Second", evaluation=stage_b(score=80))
    fake = synced_tab(tmp_path, monkeypatch)
    _, added = await sheet.job("c", company="Added", evaluation=stage_b(score=85))
    header, upper, lower = fake.values()
    upper = [*upper, *[""] * (len(HEADER) - len(upper))]
    upper[Col.NOTE] = "Written during the lookup"
    fake.funding("Added", tab_during=[header, lower, upper])
    assert main(["--config", config(tmp_path)]) == 0
    assert [row[Col.JOB_ID] for row in fake.values()[1:]] == [
        str(first),
        str(added),
        str(second),
    ]
    assert row_for(fake, first)[Col.NOTE] == "Written during the lookup"
    assert row_for(fake, first)[Col.COMPANY] == "First"
    assert row_for(fake, second)[Col.COMPANY] == "Second"
    assert {row.company: row.note for row in read_sheet(sheet.path, POLICY)} == {
        "First": "Written during the lookup",
        "Second": "",
        "Added": "",
    }


async def test_an_edit_saved_before_a_lookup_still_clears_a_refusal(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, source = await sheet.job("a", company="Known")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="with recruiter")
    assert main(["--config", config(tmp_path)]) == 0
    edit_cell(fake, source, status="to ask")
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE].startswith("Refused:")
    await sheet.job("b", company="New")
    edit_cell(fake, source, note="Back in touch")
    capsys.readouterr()
    assert main(["--config", config(tmp_path)]) == 0
    assert row_for(fake, source)[Col.SYNC_NOTE] == ""
    assert "edits applied=1 refused=0" in capsys.readouterr().out


async def test_an_edit_refused_before_a_lookup_is_refused_again_and_says_so(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, source = await sheet.job("a", company="Known")
    fake = synced_tab(tmp_path, monkeypatch)
    edit_cell(fake, source, status="with recruiter")
    assert main(["--config", config(tmp_path)]) == 0
    await sheet.job("b", company="New")
    edit_cell(fake, source, status="to ask")
    capsys.readouterr()
    assert main(["--config", config(tmp_path)]) == 0
    row = row_for(fake, source)
    assert row[Col.STATUS] == "with recruiter"
    assert row[Col.SYNC_NOTE].startswith("Refused: A row cannot go from")
    assert "edits applied=0 refused=1" in capsys.readouterr().out


async def test_a_stale_record_stays_in_force_until_a_check_replaces_it(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="Acme")
    sheet.sql(
        "INSERT INTO company_funding VALUES('acme','Acme',1,'Seed',NULL,?,"
        "'https://news.example/old','2020-01-01T00:00:00.000000Z')",
        RECENT,
    )
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Acme", exit=1)
    assert main(["--config", config(tmp_path)]) == 0
    [row] = read_sheet(sheet.path, POLICY)
    assert row.funding == f"Seed, {RECENT}"
    fake.funding("Acme")
    assert main(["--config", config(tmp_path)]) == 0
    [row] = read_sheet(sheet.path, POLICY)
    assert row.funding == f"Series A, $17M, {RECENT}"
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.funding_calls()) == 2  # noqa: PLR2004


async def test_a_recheck_that_finds_nothing_keeps_the_job_on_the_sheet_and_held(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = await sheet.job("a", company="Acme")
    sheet.sql(
        "INSERT INTO company_funding VALUES('acme','Acme',1,'Seed',NULL,?,"
        "'https://news.example/old','2020-01-01T00:00:00.000000Z')",
        RECENT,
    )
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Acme", answer={"found": False})
    assert main(["--config", config(tmp_path)]) == 0
    rows = read_sheet(sheet.path, POLICY)
    assert [row.funding for row in rows] == [f"Seed, {RECENT}"]
    assert source in held_source_ids(rows)
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.funding_calls()) == 1


async def jobs_at(sheet: Fixture, *companies: str) -> None:
    for index, company in enumerate(companies):
        await sheet.job(
            f"job-{index}", company=company, evaluation=stage_b(score=95 - index)
        )


async def test_lookups_stop_after_three_errors_in_a_row_and_say_so(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await jobs_at(sheet, "A", "B", "C", "D", "E")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=1)
    assert main(["--config", config(tmp_path)]) == FAILED
    assert [call["company"] for call in fake.funding_calls()] == ["A", "B", "C"]
    output = capsys.readouterr().out
    assert "stopped funding lookups after 3 errors in a row" in output
    assert "funding lookups=3 found=0 errors=3" in output
    assert len(fake.values()) - 1 == len("ABCDE")


async def test_a_success_resets_the_count_of_errors_in_a_row(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await jobs_at(sheet, "A", "B", "Fine", "C", "D", "E", "F")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=1)
    fake.funding("Fine")
    assert main(["--config", config(tmp_path)]) == 0
    assert [call["company"] for call in fake.funding_calls()] == [
        "A",
        "B",
        "Fine",
        "C",
        "D",
        "E",
    ]
    assert "errors=5" in capsys.readouterr().out


async def test_two_errors_in_a_row_do_not_stop_the_lookups(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await jobs_at(sheet, "A", "B", "Fine")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("*", exit=1)
    fake.funding("Fine")
    assert main(["--config", config(tmp_path)]) == 0
    assert funding_rows(sheet) == {"fine": 1}


STAR_CATCHER = {
    "round": "Series A (Star Catcher Industries; co-led by Cerberus Ventures)",
    "amount_usd": 65000000,
    "announced_on": RECENT,
    "source_url": "https://news.example/star-catcher-series-a",
    "raised_by": "Star Catcher Industries",
}


async def test_a_round_the_company_only_invested_in_takes_its_job_off_the_sheet(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sheet.job("a", company="Cerberus Capital Management")
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Cerberus Capital Management", answer=STAR_CATCHER)
    assert main(["--config", config(tmp_path)]) == 0
    assert funding_rows(sheet) == {"cerberus capital management": 0}
    assert read_sheet(sheet.path, POLICY) == []


async def wrongly_funded_cerberus(sheet: Fixture) -> int:
    _, source = await sheet.job("a", company="Cerberus Capital Management")
    sheet.sql(
        "INSERT INTO company_funding VALUES('cerberus capital management',"
        "'Cerberus Capital Management',1,?,65000000,?,?,?)",
        STAR_CATCHER["round"],
        RECENT,
        STAR_CATCHER["source_url"],
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    return source


async def test_forgetting_a_wrong_round_leaves_the_job_pending_and_held_with_no_sync(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = await wrongly_funded_cerberus(sheet)
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    forget = ["--forget-funding", "Cerberus Capital Management, LLC"]
    assert main(["--config", config(tmp_path), *forget]) == 0
    assert "forgot the funding record for cerberus capital management" in (
        capsys.readouterr().out
    )
    assert funding_rows(sheet) == {}
    assert fake.calls() == []
    assert fake.funding_calls() == []
    rows = read_sheet(sheet.path, POLICY)
    assert [row.funding_pending for row in rows] == [True]
    assert source in held_source_ids(rows)


async def test_the_next_sync_looks_a_forgotten_company_up_afresh(
    sheet: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await wrongly_funded_cerberus(sheet)
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    fake.funding("Cerberus Capital Management", answer=STAR_CATCHER)
    forget = ["--forget-funding", "Cerberus Capital Management"]
    assert main(["--config", config(tmp_path), *forget]) == 0
    assert main(["--config", config(tmp_path)]) == 0
    assert len(fake.funding_calls()) == 1
    assert funding_rows(sheet) == {"cerberus capital management": 0}
    assert read_sheet(sheet.path, POLICY) == []


@pytest.mark.usefixtures("sheet")
async def test_forgetting_a_company_with_no_record_says_so_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, tab_with())
    forget = ["--forget-funding", "Nobody"]
    assert main(["--config", config(tmp_path), *forget]) == 0
    assert "found no funding record for nobody" in capsys.readouterr().out
    assert fake.calls() == []


async def test_forgetting_works_with_no_sheet_configured(
    sheet: Fixture, tmp_path: Path
) -> None:
    await wrongly_funded_cerberus(sheet)
    forget = ["--forget-funding", "Cerberus Capital Management"]
    assert main(["--config", config(tmp_path, sheet_id=""), *forget]) == 0
    assert funding_rows(sheet) == {}


@pytest.mark.parametrize("name", ["", "   ", "!!!"])
async def test_forgetting_refuses_a_name_that_normalizes_to_nothing(
    sheet: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    await wrongly_funded_cerberus(sheet)
    install_fake_claude(tmp_path, monkeypatch, tab_with())
    assert main(["--config", config(tmp_path), "--forget-funding", name]) == FAILED
    assert "recruiter sheet sync failed" in capsys.readouterr().err
    assert funding_rows(sheet) == {"cerberus capital management": 1}


async def test_a_dry_run_cannot_be_combined_with_forgetting(
    sheet: Fixture, tmp_path: Path
) -> None:
    await wrongly_funded_cerberus(sheet)
    with pytest.raises(SystemExit):
        main(
            [
                "--config",
                config(tmp_path),
                "--dry-run",
                "--forget-funding",
                "Cerberus Capital Management",
            ]
        )
    assert funding_rows(sheet) == {"cerberus capital management": 1}
