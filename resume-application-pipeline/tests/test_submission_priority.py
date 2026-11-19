"""User completion stops application work while evidence gates submission accounting."""
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_user_submitted_closed import UserSubmittedTest
from applying_pipeline_fixture import applying_pipeline
import test_progress_monitor as fixture


class CompletionAccountingTest(UserSubmittedTest):
    def confirm(self):
        path = self.f.root / 'applications/one/user-submission-confirmation.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(job_id=1, status='submitted_user_confirmed', do_not_reapply=True,
                                       user_text='我提交了', recorded_at=fixture.NOW)))

    def test_user_submission_requires_evidence_and_reserves_potential_success(self):
        self.confirm()
        fixture.stop_with_blocker(self.f)
        snapshot = self.f.monitor.tick(force=True, now=fixture.NOW)
        self.assertEqual(snapshot['counts']['submitted'], 0)
        self.assertTrue(snapshot['jobs'][0]['reserved'])
        self.assertFalse(snapshot['jobs'][0]['submitted'])
        self.assertFalse(snapshot['jobs'][0]['submission_complete'])
        self.assertNotIn('核验回执', snapshot['jobs'][0]['next_step'])

    def test_user_submission_closeout_completes_without_official_receipt(self):
        self.confirm()
        self.f.close()
        self.f.receipt(tabs_closed=True, closed_tab_ids=['42'], tab_close_evidence='global openTabs checked')
        self.f.data['target_successful_submissions'] = 1
        self.f.data['ordered_candidates'] = [self.f.data['ordered_candidates'][0]]
        self.f.manifest.write_text(json.dumps(self.f.data))
        snapshot = self.f.monitor.tick(force=True, now=fixture.NOW)
        self.assertFalse(snapshot['jobs'][0]['closed'])
        self.assertFalse(snapshot['complete'])
        self.assertFalse(snapshot['jobs'][0]['submitted'])


class CompletionExecutionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pipeline = applying_pipeline(tmp.name)
        self.config = self.pipeline.config
        with sqlite3.connect(self.config.jobfeed_db) as db:
            for name, value in [('company', 'Acme'), ('title', 'Engineer'), ('jd_text', 'Synthetic job'), ('url', 'https://example.com/jobs/1')]:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
                db.execute(f'UPDATE jobs SET {name}=? WHERE id=1', (value,))

    def confirm(self):
        (self.config.applications_root/'one/user-submission-confirmation.json').write_text(json.dumps(dict(
            job_id=1, status='submitted_user_confirmed', do_not_reapply=True, user_text='已提交', recorded_at=fixture.NOW)))

    def test_batch_success_accounting(self):
        from pipeline.batch import BatchPipeline
        self.confirm()
        self.pipeline.__class__ = BatchPipeline
        self.pipeline.direct_application_mode = True
        self.pipeline.batch_ids = (1,)
        self.pipeline.batch_target = 2
        with patch.object(self.pipeline.jobfeed, 'is_applied', return_value=False):
            self.assertEqual(self.pipeline.successes(), 0)
            self.assertTrue(self.pipeline.can_start_application())
            self.pipeline.batch_target = 1
            self.assertFalse(self.pipeline.can_start_application())

    def test_user_submission_without_artifact_does_not_sync_or_close_tabs(self):
        self.confirm()
        with patch.object(self.pipeline.jobfeed, 'record_applied') as sync:
            result = self.pipeline.finish_submission(1)
        sync.assert_not_called()
        self.assertEqual(result['status'], 'submission_unknown')
        self.assertFalse((self.config.applications_root/'one/submission-receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
