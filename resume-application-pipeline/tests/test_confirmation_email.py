from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest import mock

from pipeline.applier_runtime import ApplierRuntime
from pipeline.confirmation_email import record

URL = 'https://jobs.ashbyhq.com/acme/1'
MESSAGE_ID = '<ack-1@email.amazonses.com>'
REQUESTED = '2026-10-31T15:55:38.926177+00:00'


def email(date, body='Please know that your application has been received.\n', subject='Thank You for Applying to Acme'):
    return (f'From: Acme Hiring Team <no-reply@ashbyhq.com>\r\nTo: applicant@example.net\r\nSubject: {subject}\r\n'
            f'Message-ID: {MESSAGE_ID}\r\nDate: {date}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}').encode()


class RecordConfirmationEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'applications' / 'job-1'
        self.folder.mkdir(parents=True)
        (self.folder / 'submission-receipt.json').write_text(json.dumps({'tab_policy': 'keep_open'}))
        item = SimpleNamespace(job_id=1, application_folder_relpath='job-1')
        self.pipeline = SimpleNamespace(
            queue=SimpleNamespace(get=lambda job_id: item), _folder=lambda item: self.folder,
            jobfeed=SimpleNamespace(get_job=lambda job_id: SimpleNamespace(url=URL, company='acme')),
            config=SimpleNamespace(resume_root=self.root))
        self.store = ApplierRuntime(self.root)
        self.store.bind(1, 'owner-1', 'scope')

    def attempt(self, source='discord'):
        with mock.patch('pipeline.applier_runtime._now', return_value=REQUESTED):
            self.store.authorize_submit(1, source_kind=source, event_id='approval-1')
            self.store.request_submit(1, 'owner-1')
            self.store.record_submit_outcome(1, 'owner-1', 'unknown', 'clicked once')
        return 'Sat, 31 Oct 2026 15:55:50 +0000'

    def test_an_email_for_the_approved_attempt_is_saved_and_settles_the_unknown_submit(self):
        raw = email(self.attempt())
        result = record(self.pipeline, 1, raw, source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual((result['status'], result['kind']), ('confirmed', 'approved_submit_email'))
        self.assertEqual(result['approval']['event_id'], 'approval-1')
        self.assertEqual(result['submit'], {'state': 'submitted', 'changed': True})
        self.assertEqual((self.folder / 'confirmation.eml').read_bytes(), raw)
        receipt = json.loads((self.folder / 'submission-receipt.json').read_text())
        self.assertEqual(receipt['tab_policy'], 'keep_open')
        self.assertEqual(receipt['confirmation']['capture'], {'tool': 'mail RAW', 'source_id': MESSAGE_ID})
        self.assertEqual([row['kind'] for row in self.store.outbox(pending=True)], ['jobfeed_sync'])

    def test_an_auto_submitted_attempt_is_recorded_and_synced_like_a_dashboard_confirmed_one(self):
        raw = email(self.attempt(source='auto'))
        result = record(self.pipeline, 1, raw, source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual((result['status'], result['kind'], result['approval']['source']), ('confirmed', 'approved_submit_email', 'auto'))
        self.assertEqual(result['submit'], {'state': 'submitted', 'changed': True})
        self.assertEqual([row['kind'] for row in self.store.outbox(pending=True)], ['jobfeed_sync'])

    def test_a_rejected_email_leaves_the_folder_and_the_attempt_unchanged(self):
        raw = email(self.attempt(), subject='Thank you for applying')
        before = (self.folder / 'submission-receipt.json').read_bytes()
        result = record(self.pipeline, 1, raw, source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['errors'], {'confirmation_email': 'email_job_link_required', 'approved_submit_email': 'email_company_required'})
        self.assertFalse((self.folder / 'confirmation.eml').exists())
        self.assertEqual((self.folder / 'submission-receipt.json').read_bytes(), before)
        self.assertEqual(self.store.submit_state(1)['state'], 'unknown')

    def test_without_an_attempt_the_email_is_rejected(self):
        result = record(self.pipeline, 1, email('Sat, 31 Oct 2026 15:55:50 +0000'), source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual(result['errors']['approved_submit_email'], 'submit_approval_required')

    def test_an_email_with_the_posting_link_needs_no_approval(self):
        raw = email('Sat, 31 Oct 2026 15:55:50 +0000', body=f'Your application has been received for {URL}\n')
        result = record(self.pipeline, 1, raw, source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual((result['kind'], result['approval'], result['submit']), ('confirmation_email', None, {'state': None, 'changed': False}))

    def test_a_different_saved_email_is_never_overwritten(self):
        (self.folder / 'confirmation.eml').write_bytes(b'other')
        with self.assertRaises(ValueError):
            record(self.pipeline, 1, email('Sat, 31 Oct 2026 15:55:50 +0000'), source_url='https://mail.example/1', capture_tool='mail RAW')
        self.assertEqual((self.folder / 'confirmation.eml').read_bytes(), b'other')


if __name__ == '__main__':
    unittest.main()
