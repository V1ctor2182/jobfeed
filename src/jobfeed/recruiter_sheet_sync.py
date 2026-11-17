"""One sync run between the user's Google Sheet and Jobfeed's recruiter sheet.

See docs/recruiter-sheet.md. The dashboard runs it after each evaluation, and this
command runs it by hand. It starts no applications.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jobfeed.adapters.funding_lookup import FundingLookup, FundingLookupError
from jobfeed.adapters.sheet_courier import SheetCourier
from jobfeed.config import Settings, load_settings
from jobfeed.domain.company_names import normalize_company_name
from jobfeed.domain.recruiter_sheet import SheetEdit, SheetPolicy
from jobfeed.domain.recruiter_sheet_sync import (
    EditOutcomes,
    has_expected_header,
    plan_sync,
    preview_edits,
    readback_mismatches,
    sheet_edits,
)
from jobfeed.recruiter_sheet import (
    apply_edits,
    forget_funding,
    funding_questions,
    read_sheet,
    save_funding,
    utc_today,
)

LOCK_NAME = "recruiter-sheet-sync.lock"
MAX_LOOKUPS = 10
LOOKUP_SECONDS = 600
MAX_ERRORS_IN_A_ROW = 3
MIN_LOOKUPS_TO_FAIL_RUN = 2


class SheetSyncError(RuntimeError):
    """The run stopped for a reason the log should state."""


@dataclass
class _Tally:
    applied: int = 0
    refused: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    lookups: int = 0
    lookups_found: int = 0
    lookup_errors: int = 0
    lookup_cost_usd: float = 0.0
    last_lookup_error: str = ""


def _look_up_funding(
    settings: Settings, tally: _Tally, clock: Callable[[], float] = time.monotonic
) -> None:
    """Check funding for up to MAX_LOOKUPS companies.

    Starts none after the time limit or after MAX_ERRORS_IN_A_ROW failed lookups.
    """
    db_path = settings.db.path
    today = utc_today()
    lookup = FundingLookup(today)
    policy = settings.recruiter_sheet.to_domain()
    started = clock()
    errors_in_a_row = 0
    for question in funding_questions(db_path, policy, today, MAX_LOOKUPS):
        if clock() - started >= LOOKUP_SECONDS:
            break
        tally.lookups += 1
        try:
            funding = lookup.look_up(question.company, question.title, question.jd_text)
        except FundingLookupError as exc:
            tally.lookup_errors += 1
            tally.last_lookup_error = str(exc)
            print(
                f"recruiter sheet sync: funding lookup failed for "
                f"{question.company_key}: {exc}",
                flush=True,
            )
            errors_in_a_row += 1
            if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                print(
                    f"recruiter sheet sync: stopped funding lookups after "
                    f"{errors_in_a_row} errors in a row",
                    flush=True,
                )
                break
            continue
        finally:
            tally.lookup_cost_usd = lookup.cost_usd
        errors_in_a_row = 0
        save_funding(db_path, question.company_key, question.company, funding)
        tally.lookups_found += funding is not None


def _read_tab(courier: SheetCourier) -> list[list[str]]:
    grid = courier.read()
    if not has_expected_header(grid):
        raise SheetSyncError(
            "the Jobs tab has an unexpected header, so nothing changed"
        )
    return grid


def _apply(db_path: Path, policy: SheetPolicy, edits: list[SheetEdit]) -> EditOutcomes:
    if not edits:
        return EditOutcomes()
    results = apply_edits(db_path, policy, edits)
    return EditOutcomes(
        applied=frozenset(r.real_job_id for r in results if r.outcome == "applied"),
        refused={r.real_job_id: r.problem.message for r in results if r.problem},
    )


def _save_edits(
    courier: SheetCourier, db_path: Path, policy: SheetPolicy
) -> tuple[list[list[str]], EditOutcomes]:
    grid = _read_tab(courier)
    edits = sheet_edits(grid, read_sheet(db_path, policy))
    return grid, _apply(db_path, policy, edits)


def _sync(
    settings: Settings, courier: SheetCourier, tally: _Tally, dry_run: bool
) -> None:
    """Bring the tab in step, saving edits before any lookup so they pin their jobs.

    Lookups can take minutes, so a run that made any reads the tab again and plans from
    that read.
    """
    db_path, sheet = settings.db.path, settings.recruiter_sheet
    policy = sheet.to_domain()
    if dry_run:
        grid = _read_tab(courier)
        rows = read_sheet(db_path, policy)
        rows, outcomes = preview_edits(rows, sheet_edits(grid, rows))
    else:
        grid, outcomes = _save_edits(courier, db_path, policy)
        _look_up_funding(settings, tally)
        if tally.lookups:
            grid, last = _save_edits(courier, db_path, policy)
            outcomes = EditOutcomes(outcomes.applied | last.applied, last.refused)
        rows = read_sheet(db_path, policy)
    tally.applied, tally.refused = len(outcomes.applied), len(outcomes.refused)
    plan = plan_sync(grid, rows, outcomes, sheet.google_sheet_tab_id)
    tally.added, tally.updated = plan.added, plan.updated
    tally.removed = plan.removed
    if dry_run or not plan.requests:
        return
    courier.write(plan.requests)
    mismatches = readback_mismatches(_read_tab(courier), plan)
    if mismatches:
        raise SheetSyncError(
            "the sheet does not hold what was written for job IDs "
            + ", ".join(mismatches)
        )


def _forget(config: Path, company: str) -> int:
    company_key = normalize_company_name(company)
    if not company_key:
        raise SheetSyncError("--forget-funding needs a company name, and this has none")
    db_path = load_settings(config).db.path
    outcome = "forgot the" if forget_funding(db_path, company_key) else "found no"
    print(f"recruiter sheet sync: {outcome} funding record for {company_key}")
    return 0


def failure_reason(exc: BaseException) -> str:
    """Say why a sync failed.

    Args:
        exc: The exception the sync raised.

    Returns:
        One line with no posting text in it.
    """
    return " ".join(str(exc).split()) or type(exc).__name__


def sync_once(settings: Settings, *, dry_run: bool = False) -> bool:
    """Run one sync, after any sync already running finishes.

    Waiting, not skipping, keeps a new evaluation's results from being dropped.

    Args:
        settings: Settings naming the database and the Google Sheet.
        dry_run: Read the sheet and report, with no lookup, saved edit or written cell.

    Returns:
        False when no google_sheet_id is set, so nothing ran. True after a sync.

    Raises:
        Exception: The sync failed. failure_reason(exc) says why in one line.
    """
    sheet_id = settings.recruiter_sheet.google_sheet_id
    if not sheet_id:
        print("recruiter sheet sync: no google_sheet_id is set, so it did nothing")
        return False
    with (settings.db.path.parent / LOCK_NAME).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        courier = SheetCourier(sheet_id)
        tally = _Tally()
        try:
            _sync(settings, courier, tally, dry_run)
        finally:
            prefix = "dry run, " if dry_run else ""
            print(
                f"recruiter sheet sync: {prefix}edits applied={tally.applied} "
                f"refused={tally.refused} rows added={tally.added} "
                f"updated={tally.updated} removed={tally.removed} "
                f"cost_usd={courier.cost_usd:.4f} "
                f"funding lookups={tally.lookups} found={tally.lookups_found} "
                f"errors={tally.lookup_errors} "
                f"funding_cost_usd={tally.lookup_cost_usd:.4f}",
                flush=True,
            )
    if (
        tally.lookups >= MIN_LOOKUPS_TO_FAIL_RUN
        and tally.lookup_errors == tally.lookups
    ):
        raise SheetSyncError(
            f"all {tally.lookups} funding lookups failed, the last because "
            f"{tally.last_lookup_error}"
        )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sync once.

    Args:
        argv: Command-line arguments, or None to read them from the process.

    Returns:
        0 when the run finished or there was nothing to do, 1 when it failed. A failure
        prints one line on stderr, last, and it holds no posting text.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="read the sheet and report, with no lookup, saved edit or written cell",
    )
    mode.add_argument(
        "--forget-funding",
        metavar="COMPANY",
        help="delete this company's funding record and exit without syncing",
    )
    args = parser.parse_args(argv)
    try:
        if args.forget_funding is not None:
            return _forget(args.config, args.forget_funding)
        sync_once(load_settings(args.config), dry_run=args.dry_run)
    except Exception as exc:
        print(
            f"recruiter sheet sync failed: {failure_reason(exc)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
