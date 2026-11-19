from datetime import datetime, timezone
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pipeline.application_rerun import ApplicationReruns, RerunError
from pipeline.application_restart import Restarts
from pipeline.resume_catalog import selected_materials, verify_selection
from tests.restart_fixture import RestartFixture


class ApplicationRestartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = RestartFixture(temp.name)
        self.service = ApplicationReruns(self.f.root, self.f.db)
        self.restarts = Restarts(self.service)
        for name, function in [('read_thread', self.f.read), ('send_existing', self.f.send)]:
            patched = patch('pipeline.application_rerun.' + name, side_effect=function)
            patched.start()
            self.addCleanup(patched.stop)
        self.bridge = Mock()
        self.bridge.probe.side_effect = lambda inventory: {
            'profile_proof': 'proof', 'expires_at': '2099-01-01T00:00:00Z',
            'matches': [{'native_tab_id': item['native_tab_id'], 'url': item['url']} for item in inventory if item['extension_instance_id'] == 'jobfeed-profile']}
        self.bridge.connect.return_value = {'job_id': 1, 'owner_thread_id': 'replacement', 'binding_id': 'bound-form', 'state': 'connected'}
        bridge = patch('pipeline.application_profile.ProfileBridge', return_value=self.bridge)
        bridge.start()
        self.addCleanup(bridge.stop)
        archived = patch('pipeline.application_restart.is_archived', side_effect=lambda owner, project: self.f.threads[owner].get('archived', False))
        archived.start()
        self.addCleanup(archived.stop)

    def request(self):
        self.ident = str(uuid4())
        result = self.restarts.request('test', 1, self.ident, 'owner', dashboard_url='http://127.0.0.1:1234')
        self.f.running()
        self.profile('owner')
        return result

    def profile(self, owner):
        return self.restarts.verify_profile(self.restarts.row(self.ident), owner, {'candidates': [{
            'browser_id': 'fresh-browser', 'extension_instance_id': 'jobfeed-profile', 'profile_name': 'Work',
            'native_tab_id': 11, 'url': self.f.job.url}]})

    def pair(self):
        self.profile('replacement')
        snapshot = self.f.resources.snapshot([1])
        self.f.resources.observe_tabs(1, 'replacement', [{'browser_id': 'fresh-browser', 'tab_id': '12',
            'url': self.f.job.url, 'state': 'observed', 'observation_id': 'fresh-new-form'}], snapshot['bindings'][0]['revision'])
        return self.restarts.pair_tab(self.restarts.row(self.ident), 'replacement', {
            'browser_id': 'fresh-browser', 'native_tab_id': 12, 'url': self.f.job.url})

    def prepare(self):
        self.request()
        self.f.observe('closed')
        return self.restarts.prepare(self.restarts.row(self.ident), 'owner')

    def adopt(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        result = self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.assertEqual(result['status'], 'adopted')
        self.pair()
        return result

    def hook(self, owner, turn):
        return self.f.gate.hook_decision({'hook_event_name': 'PreToolUse', 'session_id': owner, 'turn_id': turn})

    def test_dashboard_request_and_creation_are_single_use(self):
        result = self.request()
        self.assertEqual(result['request']['state'], 'queued')
        self.restarts.request('test', 1, self.ident, 'owner', dashboard_url='http://127.0.0.1:1234')
        self.restarts.request('test', 1, str(uuid4()), 'owner')
        self.assertEqual(len(self.f.sent), 1)
        self.assertEqual(self.hook('owner', 'new-turn'), {})
        self.assertIn('hookSpecificOutput', self.hook('owner', 'old-turn'))
        self.f.observe('closed')
        prepared = self.restarts.prepare(self.restarts.row(self.ident), 'owner')
        self.assertEqual(prepared['tool'], 'create_thread')
        self.assertIn('STOP BEFORE SUBMIT', prepared['arguments']['prompt'])
        self.assertEqual(self.restarts.prepare(self.restarts.row(self.ident), 'owner')['status'], 'reconcile_only')

    def test_creation_requires_fresh_exact_tab_closure(self):
        self.request()
        with self.assertRaisesRegex(RerunError, 'verified closed'):
            self.restarts.prepare(self.restarts.row(self.ident), 'owner')
        self.f.observe('closed')
        self.assertEqual(self.restarts.prepare(self.restarts.row(self.ident), 'owner')['status'], 'official_create_required')

    def test_acknowledgment_transfers_same_resume_and_only_exact_preparation_turn(self):
        self.adopt()
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'replacement')
        self.assertEqual(selected_materials(self.f.folder, owner='replacement')['resume_id'], 'original')
        self.assertEqual((self.f.folder / 'selected.pdf').read_bytes(), self.f.pdf)
        self.assertFalse(json.loads((self.f.folder / 'application-task.json').read_text())['submit_authorized'])
        self.assertEqual(self.hook('replacement', 'replacement-turn'), {})
        self.assertIn('hookSpecificOutput', self.hook('replacement', 'other-turn'))
        result = self.restarts.await_adoption(self.restarts.row(self.ident), 'replacement')
        self.assertEqual(result['turn_id'], 'replacement-turn')
        self.assertEqual(verify_selection(self.f.config, str(self.f.scope), 1, 'replacement',
            restart_request=self.ident, turn_id='replacement-turn')['owner'], 'replacement')
        with sqlite3.connect(self.f.root / '.pipeline/blockers.sqlite3') as db:
            self.assertEqual(db.execute('SELECT active FROM dq_scopes').fetchone()[0], 0)
        self.assertEqual(json.loads(self.f.scope.with_name('test-handoff.json').read_text())['status'], 'stopped_by_user')

    def test_new_stop_revokes_replacement_verification(self):
        self.adopt()
        self.f.gate.cancel_scope(str(self.f.scope), 'second-stop')
        self.assertIn('hookSpecificOutput', self.hook('replacement', 'replacement-turn'))
        with self.assertRaisesRegex(ValueError, 'live restart'):
            verify_selection(self.f.config, str(self.f.scope), 1, 'replacement',
                             restart_request=self.ident, turn_id='replacement-turn')

    def test_unknown_create_provenance_never_changes_owner(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        self.f.threads['replacement']['turns'][0]['items'][0]['output'] = '<codex_delegation>unverified</codex_delegation>'
        result = self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.assertEqual(result['status'], 'receipt_pending')
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')
        self.assertEqual(json.loads((self.f.folder / 'resume-source.json').read_text()), self.f.selection)

    def test_ended_replacement_initial_turn_is_not_adopted(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        self.f.threads['replacement']['turns'][0]['status'] = 'completed'
        with self.assertRaisesRegex(RerunError, 'ended before adoption'):
            self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')
        self.assertEqual(json.loads((self.f.folder / 'resume-source.json').read_text()), self.f.selection)

    def test_completed_turn_waits_for_old_chat_archive_before_next_restart(self):
        self.adopt()
        self.f.threads['replacement']['turns'][0]['status'] = 'completed'
        self.f.threads['replacement']['status'] = {'type': 'idle'}
        self.assertFalse(self.service.status('test', 1)['eligible'])
        self.assertEqual(self.hook('owner', 'new-turn'), {})
        self.f.thread['archived'] = True
        self.f.thread['turns'][-1]['status'] = 'completed'
        result = self.service.status('test', 1)
        self.assertEqual(result['request']['state'], 'completed')
        self.assertTrue(result['eligible'])

    def test_stale_preparation_artifact_cannot_excuse_later_uncertain_turn(self):
        self.f.thread['turns'][0]['items'] = [{'type': 'agentMessage', 'phase': 'final_answer',
            'text': '申请进度：等待验证\n说明：准备阶段工具停止，未提交'}]
        started = datetime.fromisoformat(self.f.thread['turns'][0]['fixture_started'])
        proof = {'job_id': 1, 'job_url': self.f.job.url, 'recorded_at': started.isoformat(),
                 'submit_attempted': False, 'submit_clicked': False, 'status': 'pre_submit_draft_incomplete'}
        (self.f.folder / 'pre-submit-result.json').write_text(json.dumps(proof))
        self.assertTrue(self.service.status('test', 1)['eligible'])
        self.f.thread['turns'].append({'id': 'later-unknown', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': '申请进度：等待验证\n说明：新一轮结果尚未核实'}],
            'fixture_started': datetime.now(timezone.utc).isoformat()})
        self.assertFalse(self.service.status('test', 1)['eligible'])

    def test_revocation_after_resume_transfer_restores_old_owner_materials(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        from pipeline import resume_catalog
        original = resume_catalog.transfer_selection
        def revoke_after_transfer(*args):
            result = original(*args)
            self.f.gate.cancel_scope(str(self.f.scope), 'cancel-adoption')
            return result
        with patch.object(resume_catalog, 'transfer_selection', side_effect=revoke_after_transfer):
            with self.assertRaises(RerunError):
                self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.service.status('test', 1)
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')
        self.assertEqual(selected_materials(self.f.folder, owner='owner')['resume_id'], 'original')

    def test_cancelled_unknown_creation_cannot_start_another_replacement(self):
        self.prepare()
        self.f.gate.cancel_scope(str(self.f.scope), 'cancel-unknown-create')
        with self.assertRaisesRegex(RerunError, 'creation or adoption'):
            self.restarts.request('test', 1, str(uuid4()), 'owner')
        self.assertEqual(len(self.f.sent), 1)

    def test_partial_adoption_retries_same_candidate_without_creating_again(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        from pipeline import resume_catalog
        original = resume_catalog.transfer_selection
        def crash_after_transfer(*args):
            original(*args)
            raise OSError('interrupted after transfer')
        with patch.object(resume_catalog, 'transfer_selection', side_effect=crash_after_transfer):
            with self.assertRaises(OSError):
                self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')
        self.assertEqual(self.hook('owner', 'new-turn'), {})
        self.assertIsNone(self.f.gate.state('replacement'))
        result = self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.assertEqual(result['status'], 'adopted')
        self.assertEqual(len(self.f.sent), 1)

    def test_completed_incomplete_preparation_can_be_restarted_again(self):
        self.adopt()
        turn = self.f.threads['replacement']['turns'][0]
        turn['items'].append({'type': 'agentMessage', 'phase': 'final_answer',
                             'text': '申请进度：等待验证\n说明：未提交，仍有未批准答案'})
        (self.f.folder / 'pre-submit-result.json').write_text(json.dumps({
            'job_id': 1, 'job_url': self.f.job.url, 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'submit_attempted': False, 'submit_clicked': False, 'status': 'pre_submit_draft_incomplete'}))
        turn['status'] = 'completed'
        self.f.threads['replacement']['status'] = {'type': 'idle'}
        self.f.thread['archived'] = True
        self.f.thread['turns'][-1]['status'] = 'completed'
        self.assertTrue(self.service.status('test', 1)['eligible'])
        result = self.restarts.request('test', 1, str(uuid4()), 'replacement', dashboard_url='http://127.0.0.1:1234')
        self.assertEqual(result['request']['state'], 'queued')
        self.assertEqual(len(self.f.sent), 2)

    def test_reconcile_cannot_regress_prepare_that_finishes_during_host_read(self):
        self.request()
        self.f.observe('closed')
        relay_turn = self.restarts.relay_turn
        advanced = False
        def prepare_during_read(row, **kwargs):
            nonlocal advanced
            observed = relay_turn(row, **kwargs)
            if not advanced:
                advanced = True
                self.restarts.prepare(self.restarts.row(self.ident), 'owner')
            return observed
        with patch.object(self.restarts, 'relay_turn', side_effect=prepare_during_read):
            self.restarts.reconcile(self.restarts.row(self.ident))
        self.assertEqual(self.restarts.row(self.ident)['phase'], 'create_unknown')
        self.assertEqual(self.restarts.prepare(self.restarts.row(self.ident), 'owner')['status'], 'reconcile_only')

    def test_stale_observations_and_errors_cannot_overwrite_later_lifecycle(self):
        self.request()
        old = self.restarts.row(self.ident)
        for phase, state, candidate, turn in [
            ('create_unknown', 'running', None, None),
            ('candidate_received', 'running', 'replacement', None),
            ('adopting', 'running', 'replacement', 'replacement-turn'),
            ('adopted', 'running', 'replacement', 'replacement-turn'),
            ('filling', 'running', 'replacement', 'replacement-turn'),
            ('completed', 'completed', 'replacement', 'replacement-turn'),
        ]:
            with self.f.gate._db() as db:
                db.execute('UPDATE application_restarts SET phase=?,candidate_owner=?,target_turn=? WHERE request_id=?', (phase, candidate, turn, self.ident))
                db.execute('UPDATE application_reruns SET state=? WHERE request_id=?', (state, self.ident))
            self.assertFalse(self.restarts.observe(old, 'cleanup', 'running'))
            self.assertFalse(self.restarts.observe(old, old['phase'], 'blocked', 'stale error'))
            actual = self.restarts.row(self.ident)
            self.assertEqual((actual['phase'], actual['state']), (phase, state))

    def test_recovery_adopts_only_existing_official_candidate_after_completed_relay(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        self.f.thread['turns'][-1]['status'] = 'completed'
        self.f.thread['status'] = {'type': 'idle'}
        with self.f.gate._db() as db:
            db.execute("UPDATE application_restarts SET phase='cleanup' WHERE request_id=?", (self.ident,))
        result = self.restarts.recover(self.restarts.row(self.ident), 'actual-operator', 'replacement')
        self.assertEqual(result['status'], 'adopted')
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'replacement')
        self.assertEqual(self.hook('replacement', 'replacement-turn'), {})
        self.assertIn('hookSpecificOutput', self.hook('owner', 'new-turn'))
        self.assertEqual(len(self.f.sent), 1)
        self.assertEqual(self.restarts.recover(self.restarts.row(self.ident), 'actual-operator', 'replacement')['status'], 'adopted')

    def test_recovery_rejects_unverified_creation_and_new_stop_without_adopting(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        output = self.f.threads['replacement']['turns'][0]['items'][0]['output']
        self.f.threads['replacement']['turns'][0]['items'][0]['output'] = output.replace('source_thread_id>owner', 'source_thread_id>other')
        with self.assertRaises(ValueError):
            self.restarts.recover(self.restarts.row(self.ident), 'actual-operator', 'replacement')
        self.assertIsNone(self.restarts.row(self.ident)['candidate_owner'])
        self.f.threads['replacement']['turns'][0]['items'][0]['output'] = output
        self.f.gate.cancel_scope(str(self.f.scope), 'stop-before-recovery')
        with self.assertRaises(RerunError):
            self.restarts.recover(self.restarts.row(self.ident), 'actual-operator', 'replacement')
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')

    def test_revocation_before_owner_commit_restores_task_and_resource_contracts(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        task = json.loads((self.f.folder / 'application-task.json').read_text())
        resources = json.loads((self.f.folder / 'resource-contract.json').read_text())
        from pipeline import resume_catalog
        publish = resume_catalog.publish
        def revoke_after_contract(path, data):
            publish(path, data)
            if path == self.f.folder / 'resource-contract.json':
                self.f.gate.cancel_scope(str(self.f.scope), 'cancel-before-commit')
        with patch.object(resume_catalog, 'publish', side_effect=revoke_after_contract):
            with self.assertRaises(RerunError):
                self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')
        self.service.status('test', 1)
        self.assertEqual(self.f.queue.get(1).codex_application_session_id, 'owner')
        self.assertEqual(json.loads((self.f.folder / 'application-task.json').read_text()), task | {'submit_authorized': False})
        self.assertEqual(json.loads((self.f.folder / 'resource-contract.json').read_text()), resources)
        self.assertEqual(selected_materials(self.f.folder, owner='owner')['resume_id'], 'original')


if __name__ == '__main__':
    unittest.main()
