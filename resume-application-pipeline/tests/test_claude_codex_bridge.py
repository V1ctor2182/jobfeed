import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from html import escape
import json
import multiprocessing
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from pipeline.claude_codex_bridge import InspectionBridge, read_thread, queue_message, read_turn_lifecycle


URL = 'https://jobs.ashbyhq.com/fixture/role-1'
SECOND_URL = 'https://jobs.ashbyhq.com/fixture/role-2'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jmL8AAAAASUVORK5CYII=')


def admission(root, scope, start, results):
    start.wait(10)
    results.put(InspectionBridge(root, scope).relay_next('relay')['status'])


class BridgeFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.documents = {'relay': {'thread': {'id': 'relay', 'originator': 'Codex Desktop', 'cwd': str(self.root)}}}
        self.sender = Mock(return_value='queued-1')
        self.reader = Mock(side_effect=lambda thread: copy.deepcopy(self.documents[thread]))
        self.terminal_reader = Mock(side_effect=lambda path, target, turn, root: {
            'state': {'completed': 'completed', 'failed': 'interrupted', 'interrupted': 'interrupted', 'cancelled': 'interrupted'}.get(
                self.documents[target]['thread']['turns'][0]['status'], 'active'), 'source': 'host_rollout'})
        self.bridge = InspectionBridge(self.root, 'inspection-test', reader=self.reader, sender=self.sender, terminal_reader=self.terminal_reader)
        self.bridge.register(project_root=self.root, project_id='project-1', relay='relay',
                             allowed_urls=[URL, SECOND_URL], authorization='Human requested public page inspection only.')
        self.reader.reset_mock()

    def submit(self, url=URL, key='fixture-key'):
        return self.bridge.submit(url, key)

    def target(self, request_id, *, target='target-1', state='inProgress', source='relay', official=True, final=None, cua=True):
        request = self.bridge.request(request_id)
        prompt = request['prompt']
        item = {'type': 'functionCallOutput', 'id': 'fco-initial', 'namespace': 'codex_app', 'name': 'create_thread',
                'output': '<codex_delegation>\n  <source_thread_id>' + source + '</source_thread_id>\n  <input>' + escape(prompt) + '</input>\n</codex_delegation>'}
        if not official:
            item = {'type': 'userMessage', 'content': [{'text': prompt}]}
        items = [item]
        if cua:
            output = json.dumps({'content': [{'type': 'text', 'text': f'Browser tab: tab-1, Title: "Engineer", URL: "{request["url"]}".\n0 AXWebArea Engineer'}]})
            items.append({'type': 'mcpToolCall', 'id': 'call-cua-1', 'server': 'cua_repl', 'tool': 'js',
                          'status': 'completed', 'error': None, 'result': {'content': [{'type': 'text', 'text': output}], 'structuredContent': None, '_meta': {}}})
        if final is not None:
            items.append({'type': 'agentMessage', 'id': 'final-1', 'phase': 'final_answer', 'text': final})
        self.documents[target] = {'thread': {'id': target, 'originator': 'Codex Desktop', 'cwd': str(self.root),
                                            'createdAt': time.time(), 'turns': [{'id': 'turn-1', 'status': state, 'items': items}]}}
        return self.documents[target]['thread']['turns'][0]

    def final(self, request_id, **overrides):
        screenshot = self.bridge.screenshot(request_id)
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(PNG)
        result = {'request_id': request_id, 'url': self.bridge.request(request_id)['url'], 'company': 'Fixture Corp',
                  'title': 'Engineer', 'page_state': 'observed', 'browser_id': 'iab', 'tab_id': 'tab-1',
                  'evidence_png': str(screenshot), 'no_application_started': True, 'fields': [], 'blockers': []}
        result.update(overrides)
        return json.dumps(result)


class InspectionBridgeTests(BridgeFixture):
    def test_submit_replay_wakes_once_and_retains_inspection_boundaries(self):
        first = self.submit()
        replay = self.submit()
        self.assertEqual(first, replay)
        self.sender.assert_called_once()
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(first['wake_state'], 'queued')
        self.assertEqual(first['execution_mode'], 'inspection_only')
        prompt = self.bridge.request(first['request_id'])['prompt']
        self.assertIn('Do not click Apply', prompt)
        self.assertIn(str(self.bridge.screenshot(first['request_id'])), prompt)
        with self.assertRaisesRegex(ValueError, 'cannot change'):
            self.submit(SECOND_URL)
        with self.assertRaisesRegex(ValueError, 'already active'):
            self.submit(key='different-key')

    def test_unknown_wake_is_durable_and_exact_retry_does_not_send_again(self):
        self.sender.side_effect = TimeoutError('transport timeout')
        first = self.submit()
        self.assertEqual(first['wake_state'], 'wake_unknown')
        reopened = InspectionBridge(self.root, 'inspection-test', reader=self.reader, sender=self.sender)
        self.assertEqual(reopened.submit(URL, 'fixture-key'), first)
        self.assertEqual(self.sender.call_count, 1)

    def test_registration_is_pinned_and_foreign_urls_never_wake(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.submit('https://foreign.invalid/role')
        self.sender.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.bridge.register(project_root=self.root, project_id='changed-project', relay='relay', allowed_urls=[URL], authorization='Changed authorization')
        with self.assertRaisesRegex(ValueError, 'Desktop workspace'):
            other = InspectionBridge(self.root, 'other', reader=lambda _: {'thread': {'id': 'relay', 'originator': 'cli', 'cwd': str(self.root)}})
            other.register(project_root=self.root, project_id='project-1', relay='relay', allowed_urls=[URL], authorization='Human authorized')

    def test_only_registered_relay_can_admit_and_unknown_creation_never_replays(self):
        first = self.submit()
        with self.assertRaisesRegex(ValueError, 'registered relay'):
            self.bridge.relay_next('foreign-relay')
        admitted = self.bridge.relay_next('relay')
        self.assertEqual(admitted['tool'], 'create_thread')
        self.assertEqual(admitted['arguments']['target'], {'type': 'project', 'projectId': 'project-1', 'environment': {'type': 'local'}})
        self.assertEqual(self.bridge.request(first['request_id'])['state'], 'create_unknown')
        self.assertEqual(self.bridge.relay_next('relay'), {'status': 'idle', 'reconcile_only': [first['request_id']]})

    def test_multiprocess_admission_returns_one_official_creation(self):
        self.submit()
        context = multiprocessing.get_context('spawn')
        start, results = context.Event(), context.Queue()
        children = [context.Process(target=admission, args=(str(self.root), 'inspection-test', start, results)) for _ in range(2)]
        for child in children:
            child.start()
        start.set()
        statuses = [results.get(timeout=15) for _ in children]
        for child in children:
            child.join(10)
            if child.is_alive():
                child.kill()
                child.join()
                self.fail('admission process did not finish')
            self.assertEqual(child.exitcode, 0)
        self.assertCountEqual(statuses, ['official_create_required', 'idle'])

    def test_simultaneous_exact_submit_replay_sends_one_wake(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.submit(), (1, 2)))
        self.assertEqual({r['request_id'] for r in responses}, {responses[0]['request_id']})
        self.assertEqual(self.sender.call_count, 1)

    def test_candidate_receipt_survives_unavailable_transcript_then_reconciles(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.reader.side_effect = TimeoutError('transcript unavailable')
        pending = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(pending['state'], 'receipt_pending')
        self.assertEqual(pending['candidate_target'], 'target-1')
        self.assertIsNone(pending['target_thread'])
        self.assertEqual(pending['verification_error'], 'transcript_read_unavailable')
        with self.assertRaisesRegex(ValueError, 'candidate target cannot change'):
            self.bridge.acknowledge(request, 'relay', 'another-target')
        self.assertEqual(self.bridge.request(request)['candidate_target'], 'target-1')
        self.target(request)
        self.reader.side_effect = lambda thread: copy.deepcopy(self.documents[thread])
        running = self.bridge.get(request)
        self.assertEqual((running['state'], running['target_thread'], running['target_turn']), ('running', 'target-1', 'turn-1'))
        self.assertIsNone(running['verification_error'])
        self.assertTrue(running['host_evidence']['creation_verified'])
        self.assertEqual(self.bridge.relay_next('relay')['status'], 'idle')

    def test_late_transcript_failure_cannot_overwrite_a_completed_observation(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request)
        self.assertEqual(self.bridge.acknowledge(request, 'relay', 'target-1')['state'], 'running')
        read_started, release_read = Event(), Event()

        def unavailable_reader(target):
            read_started.set()
            if not release_read.wait(5):
                raise AssertionError('test did not release the older observer')
            raise TimeoutError('older transcript read timed out')

        older_observer = InspectionBridge(self.root, 'inspection-test', reader=unavailable_reader,
                                          sender=self.sender, terminal_reader=self.terminal_reader)
        with ThreadPoolExecutor(max_workers=1) as pool:
            delayed = pool.submit(older_observer.get, request)
            try:
                self.assertTrue(read_started.wait(5))
                self.target(request, state='completed', final=self.final(request))
                completed = self.bridge.get(request)
                self.assertEqual(completed['state'], 'completed')
                self.assertIsNone(completed['verification_error'])
            finally:
                release_read.set()
            late = delayed.result(timeout=5)
        self.assertEqual(late['state'], 'completed')
        self.assertIsNone(late['verification_error'])
        self.assertEqual(late['result'], {
            'request_id': request, 'url': URL, 'company': 'Fixture Corp', 'title': 'Engineer',
            'page_state': 'observed', 'browser_id': 'iab', 'tab_id': 'tab-1',
            'evidence_png': str(self.bridge.screenshot(request)), 'no_application_started': True,
            'fields': [], 'blockers': [],
        })
        self.assertEqual(late['host_evidence']['turn_lifecycle']['state'], 'completed')
        self.assertEqual(self.bridge.get(request), completed)

    def test_forged_user_prompt_and_foreign_relay_are_not_creation_evidence(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, official=False)
        forged = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(forged['state'], 'create_unknown')
        self.assertIsNone(forged['target_thread'])
        self.target(request, source='foreign-relay')
        foreign = self.bridge.get(request)
        self.assertEqual(foreign['state'], 'create_unknown')
        self.assertIsNone(foreign['target_thread'])
        self.target(request)
        self.assertEqual(self.bridge.get(request)['state'], 'running')

    def test_target_metadata_freshness_and_unique_initial_turn_are_required(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request)
        original = copy.deepcopy(self.documents['target-1'])
        for field, value in (('cwd', '/another/project'), ('originator', 'codex-cli'), ('createdAt', 1)):
            with self.subTest(field=field):
                self.documents['target-1'] = copy.deepcopy(original)
                self.documents['target-1']['thread'][field] = value
                self.assertIsNone(self.bridge.acknowledge(request, 'relay', 'target-1')['target_thread'])
        self.documents['target-1'] = copy.deepcopy(original)
        self.documents['target-1']['thread']['turns'].append(copy.deepcopy(original['thread']['turns'][0]))
        self.assertIsNone(self.bridge.acknowledge(request, 'relay', 'target-1')['target_thread'])

    def test_final_requires_same_request_url_real_screenshot_and_host_cua_observation(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='completed', final=self.final(request), cua=False)
        blocked = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(blocked['state'], 'blocked')
        self.assertIn('host-observed CUA', blocked['verification_error'])
        self.assertIsNone(blocked['result'])
        self.target(request, state='completed', final=self.final(request))
        completed = self.bridge.get(request)
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['result']['company'], 'Fixture Corp')
        self.assertEqual(completed['host_evidence']['cua_calls'], [{'item_id': 'call-cua-1', 'namespace': 'cua_repl', 'name': 'js',
                          'browser_observation': {'tab_id': 'tab-1', 'title': 'Engineer', 'url': URL}}])
        self.assertEqual(len(completed['host_evidence']['screenshot_sha256']), 64)
        self.assertEqual(self.bridge.get(request), completed)
        with self.assertRaisesRegex(ValueError, 'target cannot change'):
            self.bridge.acknowledge(request, 'relay', 'another-target')

    def test_completed_turn_without_final_waits_and_never_creates_again(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='completed')
        pending = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(pending['state'], 'running')
        self.assertEqual(pending['verification_error'], 'terminal_result_pending')
        self.assertIsNone(pending['result'])
        self.target(request, state='completed', final=self.final(request))
        self.assertEqual(self.bridge.get(request)['state'], 'completed')
        self.assertEqual(self.bridge.relay_next('relay')['status'], 'idle')

    def test_mismatched_final_is_not_accepted_or_replaced_by_unrelated_later_turn(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='completed', final=self.final(request, url=SECOND_URL))
        unrelated = copy.deepcopy(self.documents['target-1']['thread']['turns'][0])
        unrelated['id'] = 'unrelated-turn'
        unrelated['items'] = [{'type': 'agentMessage', 'phase': 'final_answer', 'text': self.final(request)}]
        self.documents['target-1']['thread']['turns'].append(unrelated)
        blocked = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(blocked['state'], 'blocked')
        self.assertIsNone(blocked['result'])

    def test_failed_turn_is_not_a_completed_inspection(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='failed', final=self.final(request))
        failed = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(failed['state'], 'failed')
        self.assertIsNone(failed['result'])

    def test_reconstructed_interrupted_status_cannot_fail_an_active_desktop_turn(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='interrupted')
        self.terminal_reader.side_effect = None
        self.terminal_reader.return_value = {'state': 'active', 'event': 'task_started', 'source': 'host_rollout'}
        running = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(running['state'], 'running')
        self.assertIsNone(running['verification_error'])
        self.assertEqual(running['host_evidence']['app_server_status'], 'interrupted')
        self.assertEqual(running['host_evidence']['turn_lifecycle']['state'], 'active')
        self.target(request, state='interrupted', final=self.final(request))
        self.terminal_reader.return_value = {'state': 'completed', 'event': 'task_complete', 'source': 'host_rollout'}
        self.assertEqual(self.bridge.get(request)['state'], 'completed')

    def test_unreadable_lifecycle_never_accepts_reconstructed_terminal_status(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.target(request, state='completed', final=self.final(request))
        self.terminal_reader.side_effect = OSError('read failed')
        running = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(running['state'], 'running')
        self.assertEqual(running['verification_error'], 'turn_lifecycle_unverified')
        self.assertIsNone(running['result'])
        self.bridge.cancel(request)
        self.assertEqual(self.bridge.get(request)['state'], 'cancel_requested')

    def test_screenshot_directory_cannot_escape_through_an_ancestor_symlink(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        final = self.final(request)
        screenshot = self.bridge.screenshot(request)
        screenshot.unlink()
        screenshot.parent.rmdir()
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external).resolve()
            (outside / 'page.png').write_bytes(PNG)
            screenshot.parent.symlink_to(outside, target_is_directory=True)
            self.target(request, state='completed', final=final)
            blocked = self.bridge.acknowledge(request, 'relay', 'target-1')
            self.assertEqual(blocked['state'], 'blocked')
            self.assertIn('artifact folder', blocked['verification_error'])

    def test_cancellation_prevents_pending_dispatch_and_does_not_claim_inflight_stop(self):
        request = self.submit()['request_id']
        cancelled = self.bridge.cancel(request)
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertFalse(cancelled['verified_stopped'])
        self.assertEqual(self.bridge.cancel(request), cancelled)
        self.assertEqual(self.bridge.relay_next('relay')['status'], 'idle')
        with self.assertRaisesRegex(ValueError, 'no admitted target'):
            self.bridge.acknowledge(request, 'relay', 'foreign-target')

    def test_cancelled_unknown_request_can_reconcile_receipt_without_resuming(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        self.bridge.cancel(request)
        self.target(request, state='completed', final=self.final(request))
        cancelled = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['target_thread'], 'target-1')
        self.assertFalse(cancelled['verified_stopped'])
        self.assertEqual(self.bridge.relay_next('relay')['status'], 'idle')

    def test_cancelled_admitted_request_keeps_url_reserved_until_original_turn_ends(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        cancelled = self.bridge.cancel(request)
        self.assertEqual(cancelled['state'], 'cancel_requested')
        self.assertEqual(self.bridge.cancel(request), cancelled)
        with self.assertRaisesRegex(ValueError, 'already active'):
            self.submit(key='second-attempt')
        self.target(request)
        self.assertEqual(self.bridge.acknowledge(request, 'relay', 'target-1')['state'], 'cancel_requested')
        with self.assertRaisesRegex(ValueError, 'already active'):
            self.submit(key='second-attempt')
        self.target(request, state='interrupted')
        self.assertEqual(self.bridge.get(request)['state'], 'cancelled')
        next_request = self.submit(key='second-attempt')
        self.assertNotEqual(next_request['request_id'], request)

    def test_cancel_and_admission_race_never_releases_unknown_creation(self):
        request = self.submit()['request_id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda method: method(), (lambda: self.bridge.cancel(request), lambda: self.bridge.relay_next('relay'))))
        saved = self.bridge.request(request)
        if saved['state'] == 'cancel_requested':
            with self.assertRaisesRegex(ValueError, 'already active'):
                self.submit(key='race-retry')
        else:
            self.assertEqual(saved['state'], 'cancelled')
        self.assertEqual(self.bridge.relay_next('relay')['status'], 'idle')

    def test_failed_cua_or_bare_function_call_cannot_prove_browser_execution(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        for evidence in (
            {'type': 'functionCall', 'id': 'attempt', 'namespace': 'cua_repl', 'name': 'js'},
            {'type': 'mcpToolCall', 'id': 'failed', 'server': 'cua_repl', 'tool': 'js', 'status': 'completed', 'error': {'message': 'failed'}, 'result': {}},
            {'type': 'mcpToolCall', 'id': 'tool-error', 'server': 'cua_repl', 'tool': 'js', 'status': 'completed', 'error': None, 'result': {'isError': True}},
        ):
            with self.subTest(evidence=evidence['id']):
                turn = self.target(request, state='completed', final=self.final(request), cua=False)
                turn['items'].insert(1, evidence)
                self.assertEqual(self.bridge.acknowledge(request, 'relay', 'target-1')['state'], 'blocked')

    def test_successful_js_print_without_browser_observation_is_not_page_evidence(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        turn = self.target(request, state='completed', final=self.final(request))
        call = turn['items'][1]
        call['result']['content'][0]['text'] = json.dumps({'content': [{'type': 'text', 'text': 'nodeRepl.write finished'}]})
        blocked = self.bridge.acknowledge(request, 'relay', 'target-1')
        self.assertEqual(blocked['state'], 'blocked')
        self.assertIsNone(blocked['result'])
        self.assertIn('browser observation', blocked['verification_error'])

    def test_provider_browser_observation_must_match_the_claimed_tab_and_page(self):
        request = self.submit()['request_id']
        self.bridge.relay_next('relay')
        for url, tab in ((SECOND_URL, 'tab-1'), (URL, 'another-tab')):
            with self.subTest(url=url, tab=tab):
                turn = self.target(request, state='completed', final=self.final(request))
                turn['items'][1]['result']['content'][0]['text'] = json.dumps({'content': [{'type': 'text', 'text': f'Browser tab: {tab}, Title: "Engineer", URL: "{url}".\n0 AXWebArea Engineer'}]})
                blocked = self.bridge.acknowledge(request, 'relay', 'target-1')
                self.assertEqual(blocked['state'], 'blocked')

    def test_foreign_scope_cannot_read_or_ack_request(self):
        request = self.submit()['request_id']
        other = InspectionBridge(self.root, 'foreign-scope', reader=self.reader, sender=self.sender)
        other.register(project_root=self.root, project_id='project-1', relay='relay', allowed_urls=[URL], authorization='Another explicitly scoped inspection')
        with self.assertRaisesRegex(ValueError, 'this scope'):
            other.get(request)
        with self.assertRaisesRegex(ValueError, 'this scope'):
            other.acknowledge(request, 'relay', 'target-1')

    def test_read_adapter_uses_only_thread_read_and_refuses_approvals(self):
        with patch('pipeline.codex_app_server.CodexAppServer') as server:
            client = server.return_value.__enter__.return_value
            client.request.return_value = {'thread': {'id': 'target-1'}}
            self.assertEqual(read_thread('target-1'), {'thread': {'id': 'target-1'}})
            server.assert_called_once_with(approve_requests=False)
            client.request.assert_called_once_with('thread/read', {'threadId': 'target-1', 'includeTurns': True}, timeout=5)
            client.start_thread.assert_not_called()
            client.run_turn.assert_not_called()
            client.resume_thread.assert_not_called()

    def test_queue_adapter_uses_existing_relay_and_never_agent_execution(self):
        with patch('pipeline.claude_codex_bridge.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout='Queued message queue-1 for thread relay', stderr='')
            self.assertEqual(queue_message('relay', 'Fixed bridge wake', self.root), 'queue-1')
            command = run.call_args.args[0]
            self.assertEqual(command[1:], ['queue', '--thread', 'relay', '--message', 'Fixed bridge wake'])

    def test_actual_cli_queue_receipt_with_sentence_period_is_verified(self):
        relay = '01a15c21-cc4f-7601-8be5-d68c592008e5'
        message = '01a15c2b-77ef-7292-9bac-bbda3f29bc51'
        with patch('pipeline.claude_codex_bridge.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout=f'Queued message {message} for thread {relay}.\n', stderr='')
            self.assertEqual(queue_message(relay, 'Independent read-only handshake', self.root), message)
            run.return_value.stdout = f'Queued message {message} for thread {relay}-foreign.\n'
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                queue_message(relay, 'Independent read-only handshake', self.root)


class HostRolloutLifecycleTests(unittest.TestCase):
    def test_only_matching_host_turn_events_establish_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / 'rollout.jsonl'
            meta = {'type': 'session_meta', 'payload': {'id': 'target', 'cwd': str(root)}}
            started = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'original'}}
            wrong = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'another-turn'}}
            path.write_text('\n'.join(json.dumps(event) for event in (meta, started, wrong)) + '\n')
            self.assertEqual(read_turn_lifecycle(str(path), 'target', 'original', root)['state'], 'active')
            with path.open('a') as stream:
                stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'original'}}) + '\n')
            self.assertEqual(read_turn_lifecycle(str(path), 'target', 'original', root)['state'], 'completed')
            self.assertEqual(read_turn_lifecycle(str(path), 'foreign', 'original', root)['state'], 'unverified')
            self.assertEqual(read_turn_lifecycle(str(path), 'target', 'original', root / 'foreign')['state'], 'unverified')

    def test_truncated_event_and_unavailable_rollout_are_not_terminal_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / 'rollout.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'target', 'cwd': str(root)}}) + '\n' +
                            json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'original'}}) + '\n' +
                            json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'original'}}))
            self.assertEqual(read_turn_lifecycle(str(path), 'target', 'original', root)['state'], 'active')
            self.assertEqual(read_turn_lifecycle(str(root / 'missing'), 'target', 'original', root)['state'], 'unverified')
            with path.open('a') as stream:
                stream.write('\n' + json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 'original'}}) + '\n')
            self.assertEqual(read_turn_lifecycle(str(path), 'target', 'original', root)['state'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
