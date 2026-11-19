from pathlib import Path
import tempfile
import unittest

from pipeline.applier_mcp import ApplierTools
from pipeline.applier_runtime import ApplierRuntime
from pipeline.applier_supervisor import drain_outbox


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ApplierRuntime(Path(self.temp.name))
        self.store.bind(1, 'owner-1', 'batch')
        self.tools = ApplierTools(self.store, 1, 'owner-1')

    def test_without_authorization_the_applier_stops_before_submit(self):
        result = self.tools.request_submit()
        self.assertFalse(result['authorized'])
        self.assertIn('ready for review', result['next'])
        self.assertIsNone(self.store.submit_state(1))

    def test_only_a_dashboard_or_test_source_can_authorize(self):
        with self.assertRaisesRegex(ValueError, 'dashboard'):
            self.store.authorize_submit(1, source_kind='model', event_id='e1')

    def test_an_unknown_outcome_is_never_clicked_again(self):
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.assertTrue(self.tools.request_submit()['authorized'])
        self.assertIn('Never click Submit again', self.tools.record_submit_outcome('unknown', 'spinner never stopped')['next'])
        again = self.tools.request_submit()
        self.assertFalse(again['authorized'])
        self.assertIn('already attempted (unknown)', again['next'])
        self.assertEqual(self.store.submit_state(1)['attempts'], 1)
        self.assertEqual(self.store.outbox(), [])

    def test_a_request_without_an_outcome_blocks_a_second_click(self):
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.tools.request_submit()
        self.assertIn('already attempted (requested)', self.tools.request_submit()['next'])

    def test_one_authorization_covers_one_attempt_even_after_a_validation_error(self):
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.tools.request_submit()
        self.tools.record_submit_outcome('not_submitted', 'page said Email is required')
        self.assertFalse(self.tools.request_submit()['authorized'])
        self.store.authorize_submit(1, source_kind='test', event_id='e2')
        self.assertTrue(self.tools.request_submit()['authorized'])
        self.assertEqual(self.store.submit_state(1)['attempts'], 2)

    def test_a_revoked_authorization_cannot_be_used(self):
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.revoke_submit_auth(1, 'e1')
        self.assertFalse(self.tools.request_submit()['authorized'])

    def test_a_submitted_outcome_needs_a_valid_receipt_when_evidence_is_configured(self):
        tools = ApplierTools(self.store, 1, 'owner-1', evidence_dir=Path(self.temp.name) / 'job', job_url='https://board.example.test/jobs/1')
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        with self.assertRaisesRegex(ValueError, 'record_confirmation first'):
            tools.record_submit_outcome('submitted', 'it said thanks')
        self.assertEqual(self.store.submit_state(1)['state'], 'requested')
        tools.record_submit_outcome('unknown', 'no saved confirmation')
        self.assertEqual(self.store.outbox(), [])

    def test_a_reply_to_a_blocker_awaiting_escalation_is_refused(self):
        request = self.store.ask(1, 'owner-1', 'blocker', 'Picker stuck', field='location', blocker_type='broken_component')
        self.assertEqual(self.store.request(request)['state'], 'escalated')
        with self.assertRaisesRegex(ValueError, 'stronger model'):
            self.store.reply(request, 'done', source_kind='discord', source_id='m1', sender_id='u1')

    def test_an_outcome_needs_a_request_first(self):
        with self.assertRaisesRegex(ValueError, 'right after an authorized request_submit'):
            self.tools.record_submit_outcome('submitted', 'confirmation page')

    def test_a_failed_sync_retries_only_the_sync(self):
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.tools.request_submit()
        self.tools.record_submit_outcome('submitted', 'Thank you page with reference 123')
        calls = []

        def flaky(job_id):
            calls.append(job_id)
            if len(calls) == 1:
                raise RuntimeError('jobfeed apply service unavailable')
        self.assertEqual([e['event'] for e in drain_outbox(self.store, {'jobfeed_sync': flaky})], ['sync_failed'])
        self.assertEqual([e['event'] for e in drain_outbox(self.store, {'jobfeed_sync': flaky})], ['synced'])
        self.assertEqual(drain_outbox(self.store, {'jobfeed_sync': flaky}), [])
        self.assertEqual(calls, [1, 1])
        self.assertEqual(self.store.submit_state(1)['state'], 'submitted')
        self.assertEqual(self.store.submit_state(1)['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
