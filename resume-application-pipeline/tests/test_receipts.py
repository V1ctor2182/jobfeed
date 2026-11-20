import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jobfeed.submission_evidence import EvidenceValidationError
from pipeline import receipts
from pipeline.applier_runtime import ApplierRuntime

URL = 'https://board.example.test/jobs/1'


class ReceiptTests(unittest.TestCase):
    """2026-11-18: two jobs' receipts showed no acknowledgement, yet requeue took them as proof of a submission."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ApplierRuntime(self.root)
        self.store.bind(1, 'owner-1', 'batch')
        self.store.authorize_submit(1, source_kind='test', event_id='approval-1')
        self.assertTrue(self.store.request_submit(1, 'owner-1')[0])
        self.store.record_submit_outcome(1, 'owner-1', 'unknown', 'The page went blank.')
        self.folder, work = self.root / 'applications/job-1', self.root / 'applications/job-1/claude-applier'
        work.mkdir(parents=True)
        (self.folder / receipts.RECEIPT).write_text('{}')
        (work / 'applier-mcp.json').write_text(json.dumps({'mcpServers': {'applier': {'args': [
            '--job', '1', '--evidence-dir', str(self.folder), '--job-url', URL, '--company', 'Acme']}}}))
        with self.store._write() as db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT, job_id INTEGER, name TEXT, config TEXT, state TEXT)')
            db.execute("INSERT INTO applier_launches VALUES('owner-1', 1, 'applier-1-owner-1', ?, 'finished')", (json.dumps({'cwd': str(work)}),))
        self.launch = self.store._read('SELECT * FROM applier_launches', ())[0]

    def judged(self, error=None):
        def check(folder, job_id, job_url, *, company=None):
            self.assertEqual((folder, job_id, job_url, company), (self.folder, 1, URL, 'Acme'), 'judged against what the Applier was given')
            if error:
                raise EvidenceValidationError(error)
        with patch('jobfeed.submission_evidence.read_confirmation', check):
            return receipts.verdict(1, self.launch)

    def test_a_receipt_is_accepted_unacknowledged_or_left_to_the_existing_rules(self):
        self.assertEqual(self.judged(), receipts.ACCEPTED)
        self.assertEqual(self.judged(receipts.NO_ACKNOWLEDGEMENT), receipts.UNACKNOWLEDGED)
        self.assertIsNone(self.judged('job_url_mismatch'), 'any other rejection proves nothing either way')
        (self.folder / receipts.RECEIPT).unlink()
        self.assertIsNone(self.judged())

    def test_an_accepted_receipt_settles_its_job_as_submitted_once_and_never_under_a_running_applier(self):
        with patch('jobfeed.submission_evidence.read_confirmation', lambda *a, **k: None):
            with self.store._write() as db:
                db.execute("UPDATE applier_launches SET state='running'")
            self.assertEqual(receipts.settle(self.store), [], 'a running Applier records its own outcome')
            with self.store._write() as db:
                db.execute("UPDATE applier_launches SET state='finished'")
            self.assertEqual(receipts.settle(self.store), [{'event': 'receipt_settled_submitted', 'job_id': 1}])
            self.assertEqual(self.store.submit_state(1)['state'], 'submitted')
            self.assertEqual([row['kind'] for row in self.store.outbox()], ['jobfeed_sync'], 'Jobfeed is synced as for any submission')
            self.assertEqual(receipts.settle(self.store), [])

    def test_an_unacknowledged_receipt_settles_nothing(self):
        def reject(*args, **kwargs):
            raise EvidenceValidationError(receipts.NO_ACKNOWLEDGEMENT)
        with patch('jobfeed.submission_evidence.read_confirmation', reject):
            self.assertEqual(receipts.settle(self.store), [])
        self.assertEqual(self.store.submit_state(1)['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
