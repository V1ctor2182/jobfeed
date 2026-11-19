import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

from pipeline.answer_bank import AnswerBank
from pipeline.config import PipelineConfig
from pipeline.dashboard_submission import DashboardSubmissions, SubmissionError
from tests.rerun_fixture import RerunFixture


class DashboardSubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = RerunFixture(Path(temporary.name) / 'runtime')
        self.config = replace(PipelineConfig.discover(self.fixture.root), jobfeed_db=self.fixture.db)
        self.service = DashboardSubmissions(self.config)
        self.request = str(uuid4())
        self.bank = AnswerBank(self.fixture.root)
        self.bank.pending('Why us?', 'The exact reviewed answer.', {'company':'Rilla','job_id':'1'})
        for name, replacement in [('read_thread',self.fixture.read), ('send_existing',self.fixture.send)]:
            p = patch('pipeline.dashboard_submission.'+name, side_effect=replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch('pipeline.dashboard_submission.selected_materials', return_value={
            'job_id':1,'scope':str(self.fixture.scope),'owner':'owner','resume_id':'original-pdf'})
        p.start()
        self.addCleanup(p.stop)

    def preview(self):
        return self.service.preview(batch_id='test',job_id=1)

    def confirm(self, preview=None):
        return self.service.confirm(request_id=self.request, expected_preview=preview or self.preview(), confirmed=True)

    def test_explicit_confirmation_approves_exact_answers_and_queues_once(self):
        preview = self.preview()
        self.assertTrue(preview['eligible'])
        self.assertEqual(preview['answers'][0]['state'],'pending_review')
        result = self.confirm(preview)
        self.assertEqual(result['state'],'queued')
        self.assertFalse(result['submitted'])
        self.assertEqual(len(self.fixture.sent),1)
        self.assertIn('Record a durable submission-attempt.json', self.fixture.sent[0][1])
        self.assertNotIn('submitEasyApplyOnce', self.fixture.sent[0][1])
        self.assertEqual(self.bank.read()['entries'][0]['versions'][-1]['state'],'approved')
        provenance = self.fixture.queue.submit_authorization(1)
        self.assertEqual(provenance['source']['kind'],'dashboard')
        self.assertNotIn('source_message_id',provenance)
        self.confirm(preview)
        self.assertEqual(len(self.fixture.sent),1)
        self.assertEqual(self.fixture.gate.state('owner')['source_id'],'original-stop')

    def test_stale_preview_cannot_approve_changed_text_or_send(self):
        preview = self.preview()
        store = self.bank.read()
        self.bank.mutate({'action':'edit','entry_id':store['entries'][0]['id'],'expected_version':1,
            'expected_revision':store['revision'],'draft':{'question':'Why us?','answer':'Different text.',
                'scope':{'kind':'job','company':'Rilla','job_id':'1'}}})
        with self.assertRaisesRegex(SubmissionError,'Preview changed'):
            self.confirm(preview)
        self.assertEqual(self.fixture.sent,[])
        self.assertEqual(self.service.status(request_id=self.request)['state'],'not_found')
        self.assertEqual(self.bank.read()['entries'][0]['versions'][-1]['state'],'pending_review')

    def test_unknown_delivery_remains_reserved_after_drift_and_never_resends(self):
        with patch('pipeline.dashboard_submission.send_existing',side_effect=TimeoutError('unknown')):
            preview = self.preview()
            self.assertEqual(self.confirm(preview)['state'],'unknown')
        with self.service.db() as db:
            db.execute('UPDATE requests SET expires_at=? WHERE id=?', ('2000-01-01T00:00:00+00:00', self.request))
        with patch('pipeline.dashboard_submission.now', return_value=datetime.now(timezone.utc)+timedelta(days=30)):
            self.assertEqual(DashboardSubmissions(self.config).status(request_id=self.request)['state'], 'unknown')
        self.fixture.gate.cancel_scope(str(self.fixture.scope),'newer-stop')
        self.assertEqual(self.service.status(request_id=self.request)['state'],'blocked')
        self.request = str(uuid4())
        with self.assertRaisesRegex(SubmissionError,'earlier submission delivery'):
            self.confirm()
        self.assertEqual(self.fixture.sent,[])

    def test_matching_live_turn_only_and_drift_revokes(self):
        self.confirm()
        self.fixture.running()
        result = self.service.status(request_id=self.request)
        self.assertEqual(result['state'],'running')
        self.assertTrue(self.service.permits('owner','new-turn',scope=str(self.fixture.scope),job=1))
        self.assertFalse(self.service.permits('owner','old-turn'))
        self.assertFalse(self.service.permits('owner','new-turn',job=2))
        self.fixture.gate.cancel_scope(str(self.fixture.scope),'new-stop')
        with self.assertRaisesRegex(SubmissionError,'changed'):
            self.service.permits('owner','new-turn')

    def test_old_queued_confirmation_survives_restart_and_keeps_exact_turn_permission(self):
        self.confirm()
        self.assertIsNone(self.service.row(self.request)['expires_at'])
        with self.service.db() as db:
            db.execute('UPDATE requests SET expires_at=? WHERE id=?',
                       ('2000-01-01T00:00:00+00:00', self.request))
        self.fixture.running()
        restarted = DashboardSubmissions(self.config)
        with patch('pipeline.dashboard_submission.now', return_value=datetime.now(timezone.utc)+timedelta(days=30)):
            self.assertEqual(restarted.status(request_id=self.request)['state'], 'running')
            self.assertTrue(restarted.permits('owner', 'new-turn'))
            self.assertFalse(restarted.permits('owner', 'old-turn'))
            from pipeline.submission_release import permits
            self.assertTrue(permits(self.config, 'owner', 'new-turn', scope=str(self.fixture.scope), job=1))
            self.fixture.gate.cancel_scope(str(self.fixture.scope), 'later-cancellation')
            self.assertFalse(permits(self.config, 'owner', 'new-turn', scope=str(self.fixture.scope), job=1))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_previously_blocked_expired_confirmation_is_not_revived(self):
        self.confirm()
        with self.service.db() as db:
            db.execute("UPDATE requests SET state='blocked',expires_at=?,reason=? WHERE id=?",
                       ('2000-01-01T00:00:00+00:00', 'Previously expired', self.request))
        self.fixture.running()
        self.assertEqual(self.service.status(request_id=self.request)['state'], 'blocked')
        self.assertFalse(self.service.permits('owner', 'new-turn'))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_preexisting_attempt_or_applied_record_disables(self):
        (self.fixture.folder / 'submission-attempt.json').write_text('{}')
        self.assertFalse(self.preview()['eligible'])
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('INSERT INTO applied(job_id) VALUES(1)')
        self.assertEqual(self.preview()['reason'],'This application is already submitted.')

    def test_expired_pending_answer_cannot_be_approved_by_confirmation(self):
        store = self.bank.read()
        store['entries'][0]['versions'][-1]['valid_until']='2000-01-01'
        self.bank._write(store)
        self.assertFalse(self.preview()['eligible'])
        self.assertIn('expired',self.preview()['reason'])
        self.assertEqual(self.fixture.sent,[])

    def test_exact_attempt_marker_only_allowed_during_bound_live_turn(self):
        self.confirm()
        self.fixture.running()
        self.service.status(request_id=self.request)
        marker = self.fixture.folder / 'submission-attempt.json'
        marker.write_text(json.dumps({'dashboard_request_id':self.request,'owner_turn_id':'new-turn'}))
        self.assertTrue(self.service.permits('owner','new-turn'))
        marker.write_text(json.dumps({'dashboard_request_id':self.request,'owner_turn_id':'other-turn'}))
        with self.assertRaisesRegex(SubmissionError,'earlier attempt'):
            self.service.permits('owner','new-turn')

    def test_formal_applied_record_is_reported_as_success_not_queueing(self):
        self.confirm()
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('INSERT INTO applied(job_id) VALUES(1)')
        result = self.service.status(request_id=self.request)
        self.assertTrue(result['submitted'])
        self.assertEqual(result['state'],'submitted')
        self.assertTrue(result['synced'])

    def test_verified_receipt_sync_failure_preserves_success_and_never_resends(self):
        self.confirm()
        (self.fixture.folder / 'submission-receipt.json').write_text('{}')
        evidence = SimpleNamespace(artifact_path=self.fixture.folder / 'confirmation.png')
        with patch('jobfeed.submission_evidence.read_confirmation',return_value=evidence) as verify, \
             patch('pipeline.orchestrator.ApplicationPipeline.finish_submission',side_effect=OSError('sync unavailable')) as finish:
            result = self.service.status(request_id=self.request)
            repeated = self.service.status(request_id=self.request)
        self.assertTrue(result['submitted'])
        self.assertFalse(result['synced'])
        self.assertEqual(result['state'],'sync_pending')
        self.assertEqual(verify.call_count,2)
        verify.assert_called_with(self.fixture.folder,1,'https://example.test/application',company='Rilla')
        self.assertEqual(finish.call_count,2)
        self.assertTrue(repeated['submitted'])
        self.assertIn('finish-submission',result['next_action']['command'])
        self.assertEqual(len(self.fixture.sent),1)

    def test_verified_receipt_transitions_to_formally_synced(self):
        self.confirm()
        (self.fixture.folder / 'submission-receipt.json').write_text('{}')
        def sync(job):
            with sqlite3.connect(self.fixture.db) as db:
                db.execute('INSERT INTO applied(job_id) VALUES(?)',(job,))
            return {'status':'jobfeed_synced'}
        evidence = SimpleNamespace(artifact_path=self.fixture.folder / 'confirmation.png')
        with patch('jobfeed.submission_evidence.read_confirmation',return_value=evidence), \
             patch('pipeline.orchestrator.ApplicationPipeline.finish_submission',side_effect=sync) as finish:
            result=self.service.status(request_id=self.request)
            repeated=self.service.status(request_id=self.request)
        self.assertEqual(result['state'],'submitted')
        self.assertTrue(repeated['submitted'])
        finish.assert_called_once_with(1)
        self.assertEqual(len(self.fixture.sent),1)

    def test_invalid_receipt_is_not_success_or_a_new_delivery(self):
        self.confirm()
        (self.fixture.folder / 'submission-receipt.json').write_text('{}')
        with patch('pipeline.orchestrator.ApplicationPipeline.finish_submission') as finish:
            result = self.service.status(request_id=self.request)
        self.assertFalse(result['submitted'])
        self.assertEqual(result['state'],'blocked')
        finish.assert_not_called()
        self.assertEqual(len(self.fixture.sent),1)

    def test_ended_owner_turn_does_not_allow_new_delivery(self):
        self.confirm()
        self.fixture.running()
        self.fixture.thread['turns'][-1]['status']='completed'
        self.fixture.thread['status']={'type':'idle'}
        self.assertEqual(self.service.status(request_id=self.request)['state'],'turn_ended')
        self.request=str(uuid4())
        with self.assertRaisesRegex(SubmissionError,'earlier submission delivery'):
            self.confirm()
        self.assertEqual(len(self.fixture.sent),1)


if __name__ == '__main__':
    unittest.main()
