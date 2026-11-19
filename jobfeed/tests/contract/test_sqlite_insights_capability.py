"""SQLite insights aggregation contracts."""

# ruff: noqa: PLR2004

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from jobfeed.adapters.store import _sqlite_insights
from jobfeed.domain.models_views import InsightsDay
from tests.support.sqlite_views_performance import (
    NOW,
    insert_job,
    insert_real_job,
    open_views_performance,
    record_real_applied,
    set_real_evaluation,
    utc_text,
)


async def test_insights_empty_database_has_explicit_zero_semantics(
    tmp_path: Path, monkeypatch
) -> None:
    """Empty insights return zero totals, empty mappings, and no fake days."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "empty.db")
    try:
        result = await store.insights_overview(window_days=7)

        assert result.window_days == 7
        assert result.total_jobs == 0
        assert result.ml_gate_passed_jobs is None
        assert result.evaluated_jobs == 0
        assert result.detailed_reviewed_jobs == 0
        assert result.applied_jobs == 0
        assert result.verdict_distribution == {}
        assert result.decision_distribution == {}
        assert result.daily == []
    finally:
        await lifecycle.close()


async def test_insights_counts_completed_evaluations_without_legacy_timestamps(
    tmp_path: Path, monkeypatch
) -> None:
    """Completion status is authoritative when the completion time is absent."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "legacy-evaluated.db")
    try:
        quick = await insert_real_job(
            lifecycle,
            "quick",
            discovered_at=utc_text(NOW - timedelta(days=1)),
            status="scored",
        )
        detailed = await insert_real_job(
            lifecycle,
            "detailed",
            discovered_at=utc_text(NOW - timedelta(days=1)),
            status="scored",
        )
        await set_real_evaluation(lifecycle, quick, stage_a_score=70)
        await set_real_evaluation(
            lifecycle,
            detailed,
            stage_a_score=80,
            verdict="apply",
            stage_b_status="completed",
        )
        async with lifecycle.connection() as connection:
            await connection.execute(
                "UPDATE real_job_evaluations "
                "SET stage_a_status='completed', stage_a_at=NULL"
            )

        result = await store.insights_overview(window_days=7)

        assert result.evaluated_jobs == 2
        assert result.detailed_reviewed_jobs == 1
    finally:
        await lifecycle.close()


async def test_insights_cohort_totals_and_inclusive_windowed_days(
    tmp_path: Path, monkeypatch
) -> None:
    """Cohort totals and daily buckets use one inclusive UTC window."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "insights.db")
    try:
        old = await insert_real_job(
            lifecycle,
            "old",
            discovered_at=utc_text(NOW - timedelta(days=30)),
            status="archived",
        )
        boundary = await insert_real_job(
            lifecycle,
            "boundary",
            discovered_at=utc_text(NOW - timedelta(days=2)),
            status="scored",
        )
        today = await insert_real_job(
            lifecycle,
            "today",
            discovered_at=utc_text(NOW),
            status="applied",
        )
        future = await insert_real_job(
            lifecycle,
            "future",
            discovered_at=utc_text(NOW + timedelta(seconds=1)),
            status="new",
        )
        await set_real_evaluation(
            lifecycle,
            old,
            stage_a_score=60,
            verdict=None,
            stage_b_status="skipped_below_threshold",
            stage_a_at=utc_text(NOW - timedelta(days=30)),
            ml_gate_result="pass",
        )
        await set_real_evaluation(
            lifecycle,
            boundary,
            stage_a_score=75,
            verdict="consider",
            stage_b_status="completed",
            stage_a_at=utc_text(NOW - timedelta(days=2)),
            ml_gate_result="pass",
        )
        await set_real_evaluation(lifecycle, today, ml_gate_result="fail")
        await record_real_applied(lifecycle, today, utc_text(NOW))

        result = await store.insights_overview(window_days=2)
        all_time = await store.insights_overview(window_days=None)

        assert result.total_jobs == 2
        assert result.ml_gate_passed_jobs == 1
        assert result.evaluated_jobs == 1
        assert result.applied_jobs == 1
        assert result.verdict_distribution == {"consider": 1}
        assert result.decision_distribution == {
            "applied": 1,
            "results": 1,
        }
        assert [day.day.isoformat() for day in result.daily] == [
            (NOW - timedelta(days=2)).date().isoformat(),
            NOW.date().isoformat(),
        ]
        assert (
            result.daily[0].discovered,
            result.daily[0].evaluated,
            result.daily[0].applied,
        ) == (1, 1, 0)
        assert (
            result.daily[1].discovered,
            result.daily[1].evaluated,
            result.daily[1].applied,
        ) == (1, 0, 1)
        assert all_time.window_days is None
        assert all_time.total_jobs == 3
        assert all_time.ml_gate_passed_jobs == 2
        assert all_time.evaluated_jobs == 2
        assert all_time.applied_jobs == 1
        assert all_time.verdict_distribution == {
            "below_threshold": 1,
            "consider": 1,
        }
        assert all_time.decision_distribution == {
            "applied": 1,
            "ignored": 1,
            "results": 1,
        }
        assert [day.day.isoformat() for day in all_time.daily] == [
            (NOW - timedelta(days=30)).date().isoformat(),
            (NOW - timedelta(days=2)).date().isoformat(),
            NOW.date().isoformat(),
        ]
        assert future
    finally:
        await lifecycle.close()


async def test_insights_applied_matches_the_triage_applied_group(
    tmp_path: Path, monkeypatch
) -> None:
    """Applied totals include every status shown in the Triage Applied tab."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "applied-status.db")
    try:
        current_applied = await insert_real_job(
            lifecycle,
            "current-applied",
            discovered_at=utc_text(NOW - timedelta(days=1)),
            status="applied",
        )
        historical_applied = await insert_real_job(
            lifecycle,
            "historical-applied",
            discovered_at=utc_text(NOW),
            status="interviewing",
        )
        left_applied = await insert_real_job(
            lifecycle,
            "left-applied",
            discovered_at=utc_text(NOW),
            status="shortlisted",
        )
        await record_real_applied(
            lifecycle, current_applied, utc_text(NOW - timedelta(days=1))
        )
        await record_real_applied(lifecycle, historical_applied, utc_text(NOW))
        await record_real_applied(lifecycle, left_applied, utc_text(NOW))

        result = await store.insights_overview(window_days=7)

        assert result.applied_jobs == 2
        assert result.decision_distribution == {"applied": 2, "wait": 1}
        assert result.daily == [
            InsightsDay(
                day=(NOW - timedelta(days=1)).date(),
                discovered=1,
                evaluated=0,
                applied=1,
            ),
            InsightsDay(day=NOW.date(), discovered=2, evaluated=0, applied=1),
        ]
    finally:
        await lifecycle.close()


async def _apply_through(
    lifecycle, real_job_id: int, source_job_id: int, at: str
) -> None:
    async with lifecycle.connection() as connection:
        await connection.execute(
            "INSERT INTO real_job_applications (real_job_id, source_job_id, applied_at)"
            "\n               VALUES (?, ?, ?)",
            (real_job_id, source_job_id, at),
        )


async def _source_of(lifecycle, real_job_id: int, platform: str) -> int:
    async with lifecycle.connection() as connection:
        cursor = await connection.execute(
            "SELECT representative_job_id FROM real_jobs WHERE id=?", (real_job_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        await connection.execute(
            "UPDATE jobs SET platform=? WHERE id=?", (platform, row[0])
        )
    return int(row[0])


async def test_insights_counts_applied_jobs_by_the_source_of_their_latest_application(
    tmp_path: Path, monkeypatch
) -> None:
    """Each applied job counts once, under the platform it was last applied through."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "applied-sources.db")
    try:
        today = utc_text(NOW)
        ashby = await insert_real_job(
            lifecycle, "ashby", discovered_at=today, status="applied"
        )
        await _apply_through(
            lifecycle, ashby, await _source_of(lifecycle, ashby, "ashby"), today
        )
        moved = await insert_real_job(
            lifecycle, "moved", discovered_at=today, status="interviewing"
        )
        indeed = await _source_of(lifecycle, moved, "indeed")
        greenhouse = await insert_job(
            lifecycle, "moved-greenhouse", discovered_at=today, platform="greenhouse"
        )
        await _apply_through(
            lifecycle, moved, indeed, utc_text(NOW - timedelta(days=2))
        )
        await _apply_through(
            lifecycle, moved, greenhouse, utc_text(NOW - timedelta(days=1))
        )
        await insert_real_job(
            lifecycle, "marked", discovered_at=today, status="applied"
        )
        waiting = await insert_real_job(
            lifecycle, "waiting", discovered_at=today, status="shortlisted"
        )
        await _apply_through(
            lifecycle, waiting, await _source_of(lifecycle, waiting, "ashby"), today
        )

        result = await store.insights_overview(window_days=None)

        assert result.applied_by_source == {"ashby": 1, "greenhouse": 1, "unknown": 1}
        assert sum(result.applied_by_source.values()) == result.applied_jobs
    finally:
        await lifecycle.close()


async def test_insights_window_filters_the_full_discovery_cohort(
    tmp_path: Path, monkeypatch
) -> None:
    """Seven- and 30-day insights use different discovery cohorts everywhere."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "cohort.db")
    try:
        older = await insert_real_job(
            lifecycle,
            "older",
            discovered_at=utc_text(NOW - timedelta(days=8)),
            status="applied",
        )
        recent = await insert_real_job(
            lifecycle,
            "recent",
            discovered_at=utc_text(NOW - timedelta(days=1)),
            status="interviewing",
        )
        await record_real_applied(lifecycle, older, utc_text(NOW - timedelta(days=1)))
        await record_real_applied(lifecycle, recent, utc_text(NOW))
        for job_id, gate in ((older, "pass"), (recent, None)):
            await set_real_evaluation(
                lifecycle,
                job_id,
                stage_a_score=80,
                verdict="consider",
                stage_b_status="completed",
                stage_a_at=utc_text(NOW - timedelta(days=1)),
                ml_gate_result=gate,
            )

        week = await store.insights_overview(window_days=7)
        month = await store.insights_overview(window_days=30)

        assert (week.total_jobs, month.total_jobs) == (1, 2)
        assert (week.ml_gate_passed_jobs, month.ml_gate_passed_jobs) == (None, 1)
        assert (week.evaluated_jobs, month.evaluated_jobs) == (1, 2)
        assert (week.applied_jobs, month.applied_jobs) == (1, 2)
        assert week.verdict_distribution == {"consider": 1}
        assert month.verdict_distribution == {"consider": 2}
        assert week.decision_distribution == {"applied": 1}
        assert month.decision_distribution == {"applied": 2}
        assert sum(day.applied for day in week.daily) == week.applied_jobs == 1
        assert sum(day.applied for day in month.daily) == month.applied_jobs == 2
    finally:
        await lifecycle.close()


async def test_insights_reports_real_jobs_when_source_rows_never_changed(
    tmp_path: Path, monkeypatch
) -> None:
    """A real job's status and evaluation win over its stale source posting."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "real-jobs.db")
    try:
        applied = await insert_real_job(
            lifecycle,
            "applied",
            discovered_at=utc_text(NOW - timedelta(days=1)),
            status="applied",
        )
        await set_real_evaluation(
            lifecycle,
            applied,
            stage_a_score=80,
            verdict="apply",
            stage_b_status="completed",
            stage_a_at=utc_text(NOW),
        )
        await record_real_applied(lifecycle, applied, utc_text(NOW))

        result = await store.insights_overview(window_days=7)

        assert result.total_jobs == 1
        assert result.evaluated_jobs == 1
        assert result.detailed_reviewed_jobs == 1
        assert result.applied_jobs == 1
        assert result.verdict_distribution == {"apply": 1}
        assert result.decision_distribution == {"applied": 1}
        assert result.daily == [
            InsightsDay(
                day=(NOW - timedelta(days=1)).date(),
                discovered=1,
                evaluated=0,
                applied=0,
            ),
            InsightsDay(day=NOW.date(), discovered=0, evaluated=1, applied=1),
        ]
    finally:
        await lifecycle.close()


async def test_insights_gate_count_is_unknown_when_no_job_was_gated(
    tmp_path: Path, monkeypatch
) -> None:
    """With the gate off the count is unknown, not zero passes."""
    monkeypatch.setattr(_sqlite_insights, "_utc_now", lambda: NOW)
    lifecycle, store = await open_views_performance(tmp_path / "gate-off.db")
    try:
        await insert_real_job(
            lifecycle, "ungated", discovered_at=utc_text(NOW - timedelta(days=1))
        )
        failed = await insert_real_job(
            lifecycle, "failed", discovered_at=utc_text(NOW - timedelta(days=9))
        )
        await set_real_evaluation(lifecycle, failed, ml_gate_result="fail")

        week = await store.insights_overview(window_days=7)
        all_time = await store.insights_overview(window_days=None)

        assert week.ml_gate_passed_jobs is None
        assert all_time.ml_gate_passed_jobs == 0
    finally:
        await lifecycle.close()
