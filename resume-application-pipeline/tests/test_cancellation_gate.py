"""Isolated replay tests. These do not prove browser RPC/cell interception."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pipeline.cancellation import CancellationStore, ExecutionCancelled, hook_configuration
from pipeline.codex_app_server import CodexAppServer, AppServerError, AppServerTimeout


class CancellationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = CancellationStore(self.root / 'gate.sqlite3')
        self.store.bind('batch-a', 1, 'owner')

    def evidence(self):
        return dict(scope='batch-a', job_id=1, source_thread_id='user-thread', source_message_id='message',
                    user_text='submitted', message_at='2026-10-16T12:00:00+00:00',
                    transport='test-authenticated-transport', human_verified=True)

    def test_atomic_receipt_and_cancel_survive_reopen(self):
        evidence = self.evidence()
        first = self.store.receive_verified(evidence, 'submission')
        second = CancellationStore(self.store.path).receive_verified(evidence, 'submission')
        self.assertEqual(first, second)
        with self.assertRaises(ExecutionCancelled):
            CancellationStore(self.store.path).assert_dispatch('owner')
        with self.assertRaises(ValueError):
            self.store.receive_verified({**evidence, 'user_text': 'rewritten'}, 'submission')

    def test_scope_binding_and_old_turn_blocked_cleanup_exact_turn(self):
        self.store.cancel('batch-b', 1, 'other-message')
        self.store.assert_dispatch('owner')
        with self.assertRaises(ValueError): self.store.bind('batch-b', 1, 'owner')
        self.store.cancel('batch-a', 1, 'message')
        payload = dict(hook_event_name='PreToolUse', session_id='owner', turn_id='old', tool_name='mcp__node_repl__js')
        self.assertEqual(self.store.hook_decision(payload)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.store.grant_cleanup_turn('owner', 'cleanup', 'closeout')
        self.assertEqual(self.store.hook_decision({**payload, 'turn_id': 'cleanup'}), {})
        self.assertTrue(self.store.hook_decision(payload))
        self.store.cancel('batch-a', 1, 'stop-message', reason='stopped_by_user')
        with self.assertRaises(ExecutionCancelled): self.store.assert_dispatch('owner', 'closeout')
        self.assertTrue(self.store.hook_decision({**payload, 'turn_id': 'cleanup'}))

    def test_scope_stop_survives_reopen_covers_late_binding_and_leaves_other_scope(self):
        first = self.store.cancel_scope('batch-a', 'human-stop')
        second = CancellationStore(self.store.path).cancel_scope('batch-a', 'human-stop')
        self.assertEqual(first, second)
        self.store.bind('batch-a', 2, 'late-owner')
        self.store.bind('batch-b', 3, 'other-owner')
        for owner in ('owner', 'late-owner'):
            with self.assertRaises(ExecutionCancelled): self.store.assert_dispatch(owner)
        self.store.assert_dispatch('other-owner')

    def test_command_hook_explicit_deny_and_missing_corrupt_store_fail_closed(self):
        self.store.cancel('batch-a', 1, 'message')
        script = Path(__file__).resolve().parents[1] / 'pipeline/cancellation.py'
        for database in (self.store.path, self.root/'missing.sqlite3', self.root/'corrupt.sqlite3'):
            if database.name == 'corrupt.sqlite3': database.write_text('not sqlite')
            output = subprocess.run([sys.executable, str(script), '--database', str(database)],
                input=json.dumps(dict(hook_event_name='PreToolUse', session_id='owner', turn_id='old')),
                text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(output.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        config = hook_configuration(sys.executable, script.parent.parent, self.store.path)
        self.assertEqual(config['hooks']['PreToolUse'][0]['matcher'], '*')

    def test_persistent_cancel_after_start_interrupts_and_requires_exact_terminal_event(self):
        server = CodexAppServer(); calls = []
        def request(method, params, **kw):
            calls.append(method)
            if method == 'turn/start':
                self.store.cancel('batch-a', 1, 'message')
                return {'turn': {'id': 'turn'}}
            return {}
        server.request = request
        server._wait_for = lambda *_: {'method': 'turn/completed', 'params': {'threadId': 'owner', 'turn': {'id': 'turn', 'status': 'interrupted'}}}
        with self.assertRaisesRegex(AppServerError, 'interrupted'):
            server.run_turn('owner', 'prompt', execution_gate=self.store)
        self.assertEqual(calls, ['turn/start', 'turn/interrupt'])
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT status FROM execution_stops').fetchone()[0], 'interrupted')
        with self.assertRaises(ExecutionCancelled):
            server.run_turn('owner', 'new prompt', execution_gate=self.store)
        self.assertEqual(calls, ['turn/start', 'turn/interrupt'])

    def test_stop_ack_absent_active_and_read_error_are_unknown(self):
        server = CodexAppServer()
        for value in (None, {'id': 'turn', 'status': 'inProgress'}):
            server.read_turn = Mock(return_value=value)
            self.assertEqual(server.verify_stop('owner', 'turn')['status'], 'stop_unverified')
        server.read_turn = Mock(side_effect=AppServerTimeout('unavailable'))
        self.assertEqual(server.verify_stop('owner', 'turn')['status'], 'stop_unverified')
        server.read_turn = Mock(return_value={'id':'turn', 'status':'interrupted'})
        self.assertEqual(server.verify_stop('owner', 'turn', execution_gate=self.store)['status'], 'stop_verified')

    def test_native_user_message_does_not_establish_human_origin(self):
        server = CodexAppServer()
        server.request = Mock(return_value={'thread': {'turns': [{'items': [
            {'type': 'userMessage', 'id': 'message', 'content': [{'type': 'text', 'text': 'submitted'}]}]}]}})
        source = server.read_user_update_source(json.dumps({'source_thread_id':'thread', 'source_message_id':'message'}))
        self.assertIs(source['human_verified'], False)


if __name__ == '__main__': unittest.main()
