"""SQLite insights aggregates."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import aiosqlite

from jobfeed.adapters.store._sqlite_capability_support import (
    _fetch_row,
    _fetch_rows,
    _require_utc_timestamp,
)
from jobfeed.adapters.store.sqlite_lifecycle import SqliteLifecycle
from jobfeed.domain.models_views import InsightsDay, InsightsOverview

_TOTALS_SQL = """WITH cohort AS (
    SELECT id FROM real_jobs
    WHERE (? IS NULL OR first_discovered_at>=?) AND first_discovered_at<=?
)
SELECT
    (SELECT COUNT(*) FROM cohort) AS total_jobs,
    (SELECT CASE WHEN COUNT(e.ml_gate_result)=0 THEN NULL
                 ELSE SUM(e.ml_gate_result='pass') END
        FROM real_job_evaluations e JOIN cohort c ON c.id=e.real_job_id)
        AS gate_passed,
    (SELECT COUNT(*) FROM real_job_evaluations e JOIN cohort c ON c.id=e.real_job_id
        WHERE e.stage_a_status='completed') AS evaluated,
    (SELECT COUNT(*) FROM real_job_evaluations e JOIN cohort c ON c.id=e.real_job_id
        WHERE e.stage_b_status='completed') AS detailed_reviewed,
    (SELECT COUNT(*) FROM real_job_status s JOIN cohort c ON c.id=s.real_job_id
        WHERE s.status IN
          ('applied','interviewing','offer','rejected','ghosted')) AS applied"""
_VERDICTS_SQL = """SELECT
    CASE WHEN stage_b_verdict IS NOT NULL THEN stage_b_verdict
         ELSE 'below_threshold' END AS bucket,
    COUNT(*) AS n
    FROM real_job_evaluations e JOIN real_jobs r ON r.id=e.real_job_id
    WHERE (? IS NULL OR r.first_discovered_at>=?) AND r.first_discovered_at<=?
      AND (stage_b_verdict IS NOT NULL
       OR stage_b_status='skipped_below_threshold'
    )
    GROUP BY bucket"""
_DECISIONS_SQL = """SELECT
    CASE
      WHEN s.status IN ('new','scored') THEN 'results'
      WHEN s.status IN ('shortlisted','awaiting_referral') THEN 'wait'
      WHEN s.status IN ('applied','interviewing','offer','rejected','ghosted')
        THEN 'applied'
      WHEN s.status IN ('ignored','archived') THEN 'ignored'
      ELSE NULL
    END AS bucket,
    COUNT(*) AS n
    FROM real_job_status s JOIN real_jobs r ON r.id=s.real_job_id
    WHERE (? IS NULL OR r.first_discovered_at>=?) AND r.first_discovered_at<=?
    GROUP BY bucket"""
_APPLIED_SOURCES_SQL = """SELECT
    COALESCE((SELECT j.platform FROM real_job_applications a
              JOIN jobs j ON j.id=a.source_job_id
              WHERE a.real_job_id=s.real_job_id
              ORDER BY a.applied_at DESC, a.id DESC LIMIT 1), 'unknown') AS bucket,
    COUNT(*) AS n
    FROM real_job_status s JOIN real_jobs r ON r.id=s.real_job_id
    WHERE (? IS NULL OR r.first_discovered_at>=?) AND r.first_discovered_at<=?
      AND s.status IN ('applied','interviewing','offer','rejected','ghosted')
    GROUP BY bucket"""
_DISCOVERED_DAILY_SQL = """SELECT
    substr(first_discovered_at, 1, 10) AS day, COUNT(*) AS n
    FROM real_jobs
    WHERE (? IS NULL OR first_discovered_at>=?) AND first_discovered_at<=?
    GROUP BY day"""
_EVALUATED_DAILY_SQL = """SELECT substr(e.stage_a_at, 1, 10) AS day, COUNT(*) AS n
    FROM real_job_evaluations e JOIN real_jobs r ON r.id=e.real_job_id
    WHERE (? IS NULL OR r.first_discovered_at>=?) AND r.first_discovered_at<=?
      AND (? IS NULL OR e.stage_a_at>=?) AND e.stage_a_at<=?
    GROUP BY day"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _daily_applied_counts(
    connection: aiosqlite.Connection,
    cutoff: str | None,
    now: str,
) -> list[aiosqlite.Row]:
    """Bucket the current Applied decision cohort by its latest apply event."""
    return await _fetch_rows(
        connection,
        """WITH current_applied AS (
               SELECT s.real_job_id,
                      COALESCE(MAX(h.changed_at), s.last_status_change_at) AS event_at
               FROM real_job_status s
               JOIN real_jobs r ON r.id=s.real_job_id
               LEFT JOIN real_job_status_history h
                 ON h.real_job_id=s.real_job_id AND h.to_status='applied'
               WHERE s.status IN
                 ('applied','interviewing','offer','rejected','ghosted')
                 AND (? IS NULL OR r.first_discovered_at>=?)
                 AND r.first_discovered_at<=?
               GROUP BY s.real_job_id, s.last_status_change_at
           )
           SELECT substr(event_at, 1, 10) AS day, COUNT(*) AS n
           FROM current_applied
           WHERE event_at<=?
           GROUP BY day""",
        (cutoff, cutoff, now, now),
    )


def _merge_days(
    discovered: list[aiosqlite.Row],
    evaluated: list[aiosqlite.Row],
    applied: list[aiosqlite.Row],
) -> list[InsightsDay]:
    """Merge three sparse measure series.

    Time complexity: O(n log n) for a linear merge followed by day sorting.

    Returns:
        Non-empty daily buckets sorted ascending, with missing measures zeroed.
    """
    by_day: dict[date, dict[str, int]] = {}
    for measure, records in (
        ("discovered", discovered),
        ("evaluated", evaluated),
        ("applied", applied),
    ):
        for row in records:
            day = date.fromisoformat(str(row["day"]))
            by_day.setdefault(day, {})[measure] = int(row["n"])
    return [
        InsightsDay(
            day=day,
            discovered=counts.get("discovered", 0),
            evaluated=counts.get("evaluated", 0),
            applied=counts.get("applied", 0),
        )
        for day, counts in sorted(by_day.items())
    ]


class _SqliteInsights:
    """Internal mixin implementing insights aggregation."""

    _lifecycle: SqliteLifecycle

    async def insights_overview(self, *, window_days: int | None) -> InsightsOverview:
        """Return one discovery-date cohort and its closed UTC daily series."""
        now = _utc_now()
        now_text = _require_utc_timestamp(now)
        cutoff = (
            None
            if window_days is None
            else _require_utc_timestamp(
                now - timedelta(days=window_days), "insights cutoff"
            )
        )
        async with self._lifecycle.connection() as connection:
            cohort_params = (cutoff, cutoff, now_text)
            totals = await _fetch_row(connection, _TOTALS_SQL, cohort_params)
            verdicts = await _fetch_rows(connection, _VERDICTS_SQL, cohort_params)
            decisions = await _fetch_rows(connection, _DECISIONS_SQL, cohort_params)
            sources = await _fetch_rows(connection, _APPLIED_SOURCES_SQL, cohort_params)
            discovered = await _fetch_rows(
                connection, _DISCOVERED_DAILY_SQL, cohort_params
            )
            evaluated = await _fetch_rows(
                connection,
                _EVALUATED_DAILY_SQL,
                (*cohort_params, cutoff, cutoff, now_text),
            )
            applied = await _daily_applied_counts(connection, cutoff, now_text)
        assert totals is not None
        return InsightsOverview(
            window_days=window_days,
            total_jobs=int(totals["total_jobs"]),
            ml_gate_passed_jobs=(
                None if totals["gate_passed"] is None else int(totals["gate_passed"])
            ),
            evaluated_jobs=int(totals["evaluated"]),
            detailed_reviewed_jobs=int(totals["detailed_reviewed"]),
            applied_jobs=int(totals["applied"]),
            verdict_distribution={
                str(row["bucket"]): int(row["n"]) for row in verdicts
            },
            decision_distribution={
                str(row["bucket"]): int(row["n"]) for row in decisions
            },
            daily=_merge_days(discovered, evaluated, applied),
            applied_by_source={str(row["bucket"]): int(row["n"]) for row in sources},
        )
