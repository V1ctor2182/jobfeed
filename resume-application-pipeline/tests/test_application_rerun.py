import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from pipeline.application_rerun import ApplicationReruns, RerunError
from pipeline.cancellation import ExecutionCancelled
from tests.rerun_fixture import RerunFixture

class ApplicationRerunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.f = RerunFixture(temp.name)
        self.service = ApplicationReruns(self.f.root, self.f.db)
        for name, function in [('read_thread', self.f.read), ('send_existing', self.f.send)]:
            mock = patch('pipeline.application_rerun.' + name, side_effect=function)
            mock.start(); self.addCleanup(mock.stop)
    def request(self, request_id=None):
        return self.service.request('test', 1, request_id or str(uuid4()), 'owner')
    def hook(self, owner='owner', turn='new-turn'):
        return self.f.gate.hook_decision({'hook_event_name': 'PreToolUse', 'session_id': owner, 'turn_id': turn})
    def test_click_sends_once_and_preserves_scope_and_materials(self):
        self.assertTrue(self.service.status('test', 1)['eligible'])
        request_id = str(uuid4()); response = self.request(request_id)
        self.assertEqual(response['request']['state'], 'queued')
        self.assertEqual(self.request(request_id)['request']['request_id'], request_id)
        self.assertEqual(self.request()['request']['request_id'], request_id)
        self.assertEqual(len(self.f.sent), 1)
        self.assertIn('STOP BEFORE SUBMIT', self.f.sent[0][1])
        self.assertEqual(json.loads((self.f.folder / 'resume-source.json').read_text()), {'resume_id': 'original-pdf'})
        with sqlite3.connect(self.f.root / '.pipeline/blockers.sqlite3') as db:
            self.assertEqual(db.execute('SELECT active FROM dq_scopes').fetchone()[0], 0)
        with self.assertRaises(ExecutionCancelled): self.f.gate.assert_dispatch('owner')
        self.assertIn('hookSpecificOutput', self.hook('sibling'))
    def test_only_exact_fresh_turn_gets_grant_and_completion_retires_it(self):
        self.request(); self.assertIn('hookSpecificOutput', self.hook())
        self.f.running('unrelated continuation'); self.assertIn('hookSpecificOutput', self.hook())
        self.f.thread['turns'].pop(); self.f.running(); self.assertEqual(self.hook(), {})
        self.assertIn('hookSpecificOutput', self.hook(turn='old-turn'))
        self.f.thread['turns'][-1]['status'] = 'completed'; self.f.thread['status'] = {'type': 'idle'}
        self.assertIn('hookSpecificOutput', self.hook())
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'completed')
    def test_unknown_never_resends_and_matches_later_turn(self):
        request_id = str(uuid4())
        def timeout(root, owner, prompt):
            self.f.sent.append((owner, prompt)); raise TimeoutError('after acceptance')
        with patch('pipeline.application_rerun.send_existing', side_effect=timeout):
            self.assertEqual(self.request(request_id)['request']['state'], 'unknown')
        self.assertEqual(self.request(request_id)['request']['state'], 'unknown')
        self.assertEqual(self.request()['request']['state'], 'unknown'); self.assertEqual(len(self.f.sent), 1)
        self.f.running(); self.assertEqual(self.service.status('test', 1)['request']['state'], 'running')
    def test_repeated_identical_scope_stop_revokes_grant(self):
        self.request(); self.f.running(); self.assertEqual(self.hook(), {})
        self.f.gate.cancel_scope(str(self.f.scope), 'original-stop')
        self.assertIn('hookSpecificOutput', self.hook())
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'blocked')
    def test_repeated_identical_job_stop_revokes_grant(self):
        self.request(); self.f.running()
        self.f.gate.cancel(str(self.f.scope), 1, 'original-stop', 'stopped_by_user')
        self.assertIn('hookSpecificOutput', self.hook())
    def test_verified_submission_masked_by_scope_stop_revokes(self):
        self.request(); self.f.running()
        self.f.gate.receive_verified({'source_thread_id': 'controller', 'source_message_id': 'submitted',
                                     'scope': str(self.f.scope), 'job_id': 1}, 'submission')
        self.assertIn('hookSpecificOutput', self.hook()); self.assertFalse(self.service.status('test', 1)['eligible'])
    def test_job_submission_tombstone_is_not_masked_by_scope_stop(self):
        with self.f.gate._db() as db: db.execute("UPDATE execution_cancellations SET reason='submitted_user_confirmed' WHERE job_id=1")
        self.assertFalse(self.service.status('test', 1)['eligible'])
        with self.assertRaises(RerunError): self.request()
        self.assertFalse(self.f.sent)
    def test_terminal_uncertain_and_paused_jobs_never_send(self):
        for state in ['Submitted', 'Submission Unknown', 'Needs Verification', 'Cancelled', 'Unavailable', 'Already Applied', 'Retrying', 'Retry Later']:
            with sqlite3.connect(self.f.db) as db: db.execute('UPDATE application_queue SET state=?', (state,))
            self.assertFalse(self.service.status('test', 1)['eligible'], state)
        with sqlite3.connect(self.f.db) as db:
            db.execute("UPDATE application_queue SET state='Needs Input'")
            db.execute("INSERT INTO application_advice(job_id,luna_session_id,revision,request,status,pause_scope,created_at,updated_at) VALUES(1,'owner',1,'{}','critical','job','now','now')")
        self.assertFalse(self.service.status('test', 1)['eligible']); self.assertFalse(self.f.sent)
    def test_all_submission_sources_block(self):
        for name in ['submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json']:
            path = self.f.folder / name; path.write_text('{}')
            self.assertFalse(self.service.status('test', 1)['eligible']); path.unlink()
        with sqlite3.connect(self.f.db) as db: db.execute('INSERT INTO applied VALUES(1)')
        self.assertFalse(self.service.status('test', 1)['eligible'])
    def test_busy_unreadable_and_wrong_project_block(self):
        self.f.thread['status'] = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
        self.assertFalse(self.service.status('test', 1)['eligible'])
        self.f.thread['status'] = {'type': 'idle'}; self.f.thread['cwd'] = '/tmp/elsewhere'
        self.assertFalse(self.service.status('test', 1)['eligible'])
        with patch('pipeline.application_rerun.read_thread', side_effect=OSError('unreadable')):
            self.assertFalse(self.service.status('test', 1)['eligible'])
        self.assertFalse(self.f.sent)
    def test_latest_submission_report_without_receipt_blocks(self):
        self.f.thread['turns'][-1]['items'] = [{'type': 'agentMessage', 'text': '申请进度：提交结果待确认\n说明：未知'}]
        self.assertFalse(self.service.status('test', 1)['eligible'])
    def test_changed_owner_or_membership_blocks(self):
        with self.assertRaises(RerunError): self.service.request('test', 1, str(uuid4()), 'other')
        self.f.manifest['ordered_candidates'] = [{'id': 2}]; self.f.scope.write_text(json.dumps(self.f.manifest))
        self.assertFalse(self.service.status('test', 1)['eligible'])
    def test_active_scope_and_worker_lock_block(self):
        import fcntl
        with (self.f.root / '.pipeline/worker-progress.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertFalse(self.service.status('test', 1)['eligible'])
            with self.assertRaises(RerunError): self.request()
        with sqlite3.connect(self.f.root / '.pipeline/blockers.sqlite3') as db: db.execute('UPDATE dq_scopes SET active=1')
        self.assertFalse(self.service.status('test', 1)['eligible'])
    def test_running_hook_fails_closed_if_current_thread_unreadable(self):
        self.request(); self.f.running(); self.assertEqual(self.hook(), {})
        with patch('pipeline.application_rerun.read_thread', side_effect=OSError('unreadable')):
            self.assertIn('hookSpecificOutput', self.hook())
    def test_failure_before_transport_is_blocked_not_unknown(self):
        with patch('pipeline.application_rerun.read_thread', side_effect=[self.f.thread, OSError('unreadable')]):
            self.assertEqual(self.request()['request']['state'], 'blocked')
        self.assertFalse(self.f.sent)

    def add_second_job(self, limit):
        self.f.manifest['ordered_candidates'].append({'id': 2})
        self.f.manifest['max_parallel'] = limit
        self.f.scope.write_text(json.dumps(self.f.manifest))
        with sqlite3.connect(self.f.db) as db:
            db.execute("INSERT INTO jobs VALUES(2,'Other','Engineer','JD','https://example.test/other',NULL)")
            db.execute("INSERT INTO application_queue(job_id,state,application_folder_relpath,codex_application_session_id) VALUES(2,'Needs Input','rilla','sibling')")
        with sqlite3.connect(self.f.root / '.pipeline/blockers.sqlite3') as db:
            db.execute("INSERT INTO dq_jobs VALUES(?, '2', 'active', '{}')", (str(self.f.scope),))
        sibling_path = self.f.root / 'sibling-rollout.jsonl'
        sibling_path.write_text(json.dumps({'type':'session_meta','payload':{'id':'sibling','cwd':str(self.f.root)}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'sibling-old'}})+'\n')
        return {'id':'sibling', 'path':str(sibling_path), 'originator':'Codex Desktop', 'cwd':str(self.f.root),
                'status':{'type':'idle'}, 'turns':[{'id':'sibling-old','status':'completed','items':[]}]}
    def test_pending_rerun_reserves_controller_capacity(self):
        second = self.add_second_job(1)
        with patch('pipeline.application_rerun.read_thread', side_effect=lambda owner: self.f.thread if owner == 'owner' else second):
            self.request()
            with self.assertRaisesRegex(RerunError, 'capacity'):
                self.service.request('test', 2, str(uuid4()), 'sibling')
        self.assertEqual(len(self.f.sent), 1)
    def test_running_rerun_is_not_double_counted_against_capacity(self):
        second = self.add_second_job(2)
        with patch('pipeline.application_rerun.read_thread', side_effect=lambda owner: self.f.thread if owner == 'owner' else second):
            self.request(); self.f.running()
            self.assertEqual(self.service.request('test', 2, str(uuid4()), 'sibling')['request']['state'], 'queued')
        self.assertEqual(len(self.f.sent), 2)
    def test_concurrent_clicks_send_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        admitted, release = threading.Event(), threading.Event()
        def slow_send(root, owner, prompt):
            admitted.set(); release.wait(5)
            return self.f.send(root, owner, prompt)
        with patch('pipeline.application_rerun.send_existing', side_effect=slow_send), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request)
            self.assertTrue(admitted.wait(5))
            with self.assertRaises(RerunError): self.request()
            release.set(); self.assertEqual(first.result()['request']['state'], 'queued')
        self.assertEqual(len(self.f.sent), 1)
    def test_missing_current_turn_never_reuses_cached_hook_grant(self):
        self.request(); self.f.running(); self.assertEqual(self.hook(), {})
        self.f.thread['turns'].pop()
        self.assertIn('hookSpecificOutput', self.hook())

    def test_reconstructed_interruption_cannot_end_active_host_turn(self):
        self.request(); self.f.running()
        self.f.host_states['new-turn'] = 'inProgress'
        self.f.thread['status'] = {'type':'notLoaded'}
        self.f.thread['turns'][-1]['status'] = 'interrupted'
        current = self.service.status('test', 1)
        self.assertEqual(current['request']['state'], 'running')
        self.assertFalse(current['eligible'])
        self.assertEqual(self.hook(), {})
        with self.assertRaises(RerunError): self.request()
        self.assertEqual(len(self.f.sent), 1)
    def test_host_completion_overrides_reconstructed_interruption(self):
        self.request(); self.f.running()
        self.f.host_states['new-turn'] = 'completed'
        self.f.thread['status'] = {'type':'notLoaded'}
        self.f.thread['turns'][-1]['status'] = 'interrupted'
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'completed')
        self.assertIn('hookSpecificOutput', self.hook())
    def test_reconciles_only_legacy_terminal_error_without_resending(self):
        response = self.request(); self.f.running()
        with self.f.gate._db() as db:
            db.execute("UPDATE application_reruns SET state='blocked', reason='The applier turn ended without completing.', turn_id='new-turn'")
        self.f.host_states['new-turn'] = 'inProgress'
        self.f.thread['status'] = {'type':'notLoaded'}
        self.f.thread['turns'][-1]['status'] = 'interrupted'
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'running')
        self.assertEqual(len(self.f.sent), 1)
    def test_new_identical_stop_revokes_legacy_terminal_error_before_repair(self):
        self.request(); self.f.running()
        with self.f.gate._db() as db:
            db.execute("UPDATE application_reruns SET state='blocked', reason='The applier turn ended without completing.', turn_id='new-turn'")
        self.f.gate.cancel_scope(str(self.f.scope), 'original-stop')
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'blocked')
        self.assertIn('hookSpecificOutput', self.hook())
    def test_unavailable_host_lifecycle_never_grants_stale_running_turn(self):
        self.request(); self.f.running(); self.assertEqual(self.hook(), {})
        self.f.thread['path'] = str(self.f.root / 'missing.jsonl')
        with patch('pipeline.application_rerun.read_thread', return_value=self.f.thread):
            self.assertIn('hookSpecificOutput', self.hook())
            self.assertFalse(self.service.status('test', 1)['eligible'])

    def test_legacy_terminal_repair_cannot_rebind_another_turn(self):
        self.request(); self.f.running()
        with self.f.gate._db() as db:
            db.execute("UPDATE application_reruns SET state='blocked', reason='The applier turn ended without completing.', turn_id='different-turn'")
        self.assertEqual(self.service.status('test', 1)['request']['state'], 'blocked')
        self.assertIn('hookSpecificOutput', self.hook())

if __name__ == '__main__': unittest.main()
