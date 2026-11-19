
from confirmation_fixture import write_confirmation
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.jobfeed import Jobfeed


class AppliedRecordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "jobfeed.sqlite"
        with sqlite3.connect(self.db) as c:
            c.executescript("""
                CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT,
                    jd_text TEXT, url TEXT, platform TEXT, closed_at TEXT,
                    posted_at TEXT, discovered_at TEXT, real_job_id INTEGER);
                CREATE TABLE real_jobs(id INTEGER PRIMARY KEY,representative_job_id INTEGER,official_closed_at TEXT);
                CREATE TABLE real_job_status(real_job_id INTEGER PRIMARY KEY,status TEXT);
                CREATE TABLE applied(job_id INTEGER PRIMARY KEY);
                CREATE TABLE real_job_applications(id INTEGER PRIMARY KEY,
                    real_job_id INTEGER, source_job_id INTEGER, source_applied_job_id INTEGER);
                CREATE TABLE job_status(job_id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY, state TEXT);
                CREATE TABLE job_priority_snapshot(job_id INTEGER PRIMARY KEY, priority_score REAL);
            """)
            for job_id, real_id, score in [(1,10,100),(2,10,99),(3,30,98),(4,40,97),(5,None,96)]:
                c.execute("INSERT INTO jobs VALUES (?, 'Company', 'Engineer', 'JD', 'https://example.test', 'linkedin', NULL, NULL, NULL, ?)",(job_id,real_id))
                c.execute("INSERT INTO job_priority_snapshot VALUES (?,?)",(job_id,score))
            c.executescript("INSERT INTO real_jobs VALUES (10,1,NULL),(30,3,NULL),(40,4,NULL);")
            c.execute("INSERT INTO real_job_applications VALUES (1,10,1,NULL)")
            c.execute("INSERT INTO applied VALUES (3)")
        self.feed = Jobfeed(self.db,self.root)

    def test_receipt_applies_only_to_its_source(self):
        self.assertTrue(self.feed.is_applied(1))
        self.assertFalse(self.feed.is_applied(2))
        self.assertFalse(self.feed.is_applied(4))

    def test_canonical_only_receipt_does_not_apply_to_source(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO real_job_applications VALUES (2,40,NULL,NULL)")
        self.assertFalse(self.feed.is_applied(4))

    def test_source_applied_reference_applies_only_to_that_source(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO real_job_applications VALUES (2,40,NULL,4)")
        self.assertTrue(self.feed.is_applied(4))

    def test_canonical_source_without_parent_mapping(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO real_job_applications VALUES (2,50,5,NULL)")
        self.assertTrue(self.feed.is_applied(5))

    def test_refill_keeps_unapplied_sibling_before_limit(self):
        from test_canonical_candidates import card
        rows=[card(i) for i in (1,2,3,4)]
        for row, real in zip(rows,(10,10,30,40)): row['real_job_id']=str(real)
        with patch.object(self.feed,'_canonical_page',return_value={'jobs':rows,'total':4}):
            self.assertEqual([j.id for j in self.feed.priority_jobs(limit=1)],[2])

    def test_legacy_receipt_still_recognized(self):
        self.assertTrue(self.feed.is_applied(3))

    def test_legacy_database_without_canonical_table(self):
        with sqlite3.connect(self.db) as c:
            c.execute("DROP TABLE real_job_applications")
            c.execute("ALTER TABLE jobs DROP COLUMN real_job_id")
        self.assertTrue(self.feed.is_applied(3))
        self.assertFalse(self.feed.is_applied(1))
        with self.assertRaises(sqlite3.OperationalError):
            self.feed.priority_jobs(limit=2)  # Unmigrated source schema must not silently feed new jobs.

    def test_record_applied_rejects_prose_before_formal_write(self):
        import json
        folder = self.root / 'application'
        folder.mkdir()
        (folder / 'tailored.md').write_text('Resume')
        (folder / 'submission-receipt.json').write_text(json.dumps({
            'job_id': 2, 'job_url': self.feed.get_job(2).url,
            'success_evidence': 'Application has been submitted'}))
        with patch('pipeline.jobfeed.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'confirmation_artifact_required'):
                self.feed.record_applied(2, folder)
        run.assert_not_called()
        self.assertFalse(self.feed.is_applied(2))

    def test_record_applied_does_not_shortcut_from_sibling_receipt(self):
        folder=self.root/'application';folder.mkdir();(folder/'tailored.md').write_text('Resume')
        write_confirmation(folder, 2, self.feed.get_job(2).url)
        def cli_write(*args, **kwargs):
            with sqlite3.connect(self.db) as c:
                c.execute("INSERT INTO real_job_applications VALUES (2,10,2,NULL)")
            return subprocess.CompletedProcess(args[0],0,'Application recorded','')
        with patch('pipeline.jobfeed.subprocess.run',side_effect=cli_write) as run:
            self.assertEqual(self.feed.record_applied(2,folder),'Application recorded')
            self.assertEqual(run.call_count,1)

if __name__ == '__main__':
    unittest.main()

class IdempotentApplyTest(unittest.TestCase):
    def test_existing_formal_record_does_not_repeat_cli_write(self):
        from pathlib import Path
        from unittest.mock import patch
        feed=Jobfeed(Path('/db'),Path('/repo'))
        with patch.object(feed,'is_applied',return_value=True),patch('pipeline.jobfeed.subprocess.run') as run:
            self.assertIn('already',feed.record_applied(1,Path('/missing-folder')).lower())
            run.assert_not_called()
