"""An applied job whose owner was never bound must not crash recovery or need that owner for closeout."""
import json
import tempfile
import unittest
from unittest.mock import Mock

from applying_pipeline_fixture import applying_pipeline
from confirmation_fixture import write_confirmation
from pipeline.jobfeed import Job


class UnboundOwnerCloseoutTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jobfeed = Mock(**{'get_job.return_value': Job(1, 'Acme', 'Engineer', 'Full JD', 'https://example.test/1'), 'is_applied.return_value': True})
        self.pipeline = applying_pipeline(tmp.name, folder='job-1', jobfeed=jobfeed)
        self.config = self.pipeline.config
        self.folder = self.config.applications_root / 'job-1'
        write_confirmation(self.folder, 1, jobfeed.get_job(1).url)

    def test_unbound_owner_syncs_and_keeps_tabs_open(self):
        self.assertTrue(self.pipeline.reconcile_applied(1))
        self.assertTrue(self.pipeline.reconcile_applied(1))  # Recovery tick: still no exception.
        pointer = json.loads((self.config.resume_root / '.pipeline/submission-closeouts/1.json').read_text())
        receipt = json.loads((self.folder / 'submission-receipt.json').read_text())
        self.assertTrue(pointer['complete'])
        self.assertEqual(pointer['tab_policy'], 'keep_open')
        self.assertTrue(receipt['jobfeed_synced'])
        self.assertFalse(receipt['close_pending'])
        self.assertNotIn('close_error', receipt)


if __name__ == '__main__':
    unittest.main()
