from __future__ import annotations


import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline.lifecycle import ApplicationState, InvalidTransition
from pipeline.jobfeed import Jobfeed
from pipeline.store import ApplicationQueue
from pipeline.results import parse_application_result
from pipeline.worker import WorkerSettings


class ApplicationResultTest(unittest.TestCase):
    def test_chinese_progress_preserves_submission_evidence(self) -> None:
        result = parse_application_result('申请进度：已投递\n说明：网站显示 Application Submitted，简历为当前岗位版本。')
        self.assertEqual(result['status'], 'submitted')
        self.assertIn('Application Submitted', result['success_evidence'])

    def test_chinese_blocker_is_not_mistaken_for_submission(self) -> None:
        result = parse_application_result('申请进度：需要补充信息\n说明：需要登录，尚未提交。')
        self.assertEqual(result['status'], 'needs_input')
        self.assertNotIn('success_evidence', result)

    def test_chinese_result_requires_explanation(self) -> None:
        with self.assertRaises(ValueError):
            parse_application_result('申请进度：已投递')

    def test_parser_returns_complete_top_level_result_with_nested_data(self) -> None:
        result = parse_application_result(
            'progress {"ignored":true}\n'
            '{"status":"needs_input","personal_data":{"identity":"Candidate"}}'
        )

        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["personal_data"], {"identity": "Candidate"})


class ApplicationQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "jobfeed.sqlite"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT, title TEXT, "
            "jd_text TEXT, url TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (42,'Acme','Software Engineer','Build systems','https://example.test')"
        )
        connection.commit()
        connection.close()
        self.queue = ApplicationQueue(self.db_path)
        self.queue.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_persists_lifecycle_and_recovery_references(self) -> None:
        self.queue.enqueue(42)
        self.queue.transition(42, ApplicationState.DISPATCHING)
        self.queue.transition(
            42,
            ApplicationState.TAILORING,
            claude_session_id="claude-42",
            claude_session_url="claude.ai/epitaxy/claude-42",
        )
        self.queue.transition(
            42,
            ApplicationState.RESUME_READY,
            application_folder_relpath="Acme-SWE-2026-09-03",
        )
        self.queue.transition(
            42,
            ApplicationState.APPLYING,
            codex_application_session_id="thread-42",
        )

        item = self.queue.get(42)
        self.assertEqual(item.state, ApplicationState.APPLYING)
        self.assertEqual(item.application_folder_relpath, "Acme-SWE-2026-09-03")
        self.assertEqual(item.codex_application_session_id, "thread-42")
        self.assertEqual(item.claude_session_id, "claude-42")
        self.assertEqual(item.claude_session_url, "claude.ai/epitaxy/claude-42")

        reopened = ApplicationQueue(self.db_path)
        self.assertEqual(reopened.get(42), item)

    def test_rejects_invalid_transition(self) -> None:
        self.queue.enqueue(42)
        with self.assertRaises(InvalidTransition):
            self.queue.transition(42, ApplicationState.SUBMITTED)

    def test_unavailable_stays_deduplicated_without_counting_active(self) -> None:
        self.queue.enqueue(42)
        self.queue.transition(42, ApplicationState.UNAVAILABLE)
        self.assertEqual(self.queue.get(42).state, ApplicationState.UNAVAILABLE)
        self.assertFalse(self.queue.enqueue(42))
        self.assertEqual(self.queue.list_active(), [])

    def test_skipped_job_stays_deduplicated_without_counting_as_active(self) -> None:
        self.queue.enqueue(42)
        self.queue.transition(42, ApplicationState.SKIPPED)

        self.assertEqual(self.queue.get(42).state, ApplicationState.SKIPPED)
        self.assertEqual(self.queue.list_active(), [])
        self.assertFalse(self.queue.enqueue(42))

    def test_browser_permission_denial_becomes_recoverable_input_gate(self) -> None:
        result = parse_application_result(
            "Chrome blocked access because permission was declined."
        )
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(
            result["confirmation_required"], "desktop_browser_permission"
        )


class JobfeedPriorityTest(unittest.TestCase):
    def test_official_results_still_exclude_receipts_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "jobfeed.sqlite"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, company TEXT, title TEXT,
                    jd_text TEXT, url TEXT, closed_at TEXT,
                    platform TEXT DEFAULT 'linkedin', posted_at TEXT, discovered_at TEXT
                );
                CREATE TABLE job_status (job_id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE applied (job_id INTEGER PRIMARY KEY);
                CREATE TABLE application_queue (job_id INTEGER PRIMARY KEY, state TEXT);
                CREATE TABLE job_priority_snapshot (
                    job_id INTEGER PRIMARY KEY, eligibility_status TEXT,
                    in_scope INTEGER, display_representative INTEGER,
                    blocked_rank INTEGER, queue_tier INTEGER, pending_rank INTEGER,
                    priority_score REAL, posted_sort_at TEXT
                );
                CREATE INDEX idx_job_priority_snapshot_page ON job_priority_snapshot (
                    in_scope, display_representative, blocked_rank, queue_tier,
                    pending_rank, priority_score DESC, posted_sort_at DESC, job_id DESC
                );
                """
            )
            for job_id, score in ((1, 80), (2, 95), (3, 90)):
                connection.execute(
                    "INSERT INTO jobs(id,company,title,jd_text,url,closed_at) VALUES (?,?,?,?,?,NULL)",
                    (job_id, f"Company {job_id}", "Engineer", "Full JD", f"https://example.test/{job_id}"),
                )
                connection.execute("INSERT INTO job_status VALUES (?,'scored')", (job_id,))
                connection.execute(
                    "INSERT INTO job_priority_snapshot VALUES (?,?,1,1,0,0,0,?,?)",
                    (job_id, "apply", score, f"2026-09-0{job_id}T00:00:00Z"),
                )
            connection.executescript("CREATE TABLE real_jobs(id INTEGER PRIMARY KEY,representative_job_id INTEGER,official_closed_at TEXT); CREATE TABLE real_job_status(real_job_id INTEGER PRIMARY KEY,status TEXT); ALTER TABLE jobs ADD COLUMN real_job_id INTEGER; INSERT INTO real_jobs SELECT id,id,NULL FROM jobs; UPDATE jobs SET real_job_id=id;")
            connection.execute("INSERT INTO applied VALUES (2)")
            connection.execute("INSERT INTO application_queue VALUES (3,'Tailoring')")
            connection.commit()
            connection.close()

            from unittest.mock import patch
            from test_canonical_candidates import card
            feed = Jobfeed(db_path, Path(tempdir))
            with patch.object(feed, "_canonical_page", return_value={"jobs": [card(2), card(3), card(1)], "total": 3}):
                jobs = feed.priority_jobs(limit=100)

            self.assertEqual([job.id for job in jobs], [1])


class WorkerSettingsTest(unittest.TestCase):
    def test_application_worker_supports_ten_without_changing_default(self) -> None:
        self.assertEqual(WorkerSettings(application_workers=10).application_workers, 10)
        self.assertEqual(WorkerSettings().application_workers, 4)
        with self.assertRaises(ValueError):
            WorkerSettings(application_workers=0)


if __name__ == "__main__":
    unittest.main()
