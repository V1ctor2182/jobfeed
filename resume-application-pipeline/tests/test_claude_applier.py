import sys
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from pipeline.claude_applier import ALLOWED_TOOLS, DISALLOWED_TOOLS, MCP_TIMEOUT_MS, ClaudeApplierLauncher, clean_env, has_process


class FakeClaude:
    def __init__(self):
        self.agents, self.calls, self.launch_output, self.refusal = [], [], None, None

    def run(self, argv, cwd=None, **_):
        self.calls.append(argv)
        if '--bg' in argv and self.refusal:
            return subprocess.CompletedProcess(argv, 1, '', self.refusal)
        if '--bg' in argv:
            session = str(uuid.uuid4())
            self.agents.append({'id': session[:8], 'sessionId': session, 'name': argv[argv.index('-n') + 1], 'cwd': str(cwd), 'state': 'working', 'pid': 1})
            out = self.launch_output if self.launch_output is not None else f'backgrounded · {session[:8]} · x\n'
            return subprocess.CompletedProcess(argv, 0, out, '')
        if argv[1:3] == ['agents', '--json']:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.agents), '')
        if argv[1] == 'stop':
            next(a for a in self.agents if a['id'] == argv[2])['state'] = 'stopped'
            return subprocess.CompletedProcess(argv, 0, f'stopped {argv[2]}\n', '')
        raise AssertionError(argv)

    def attach(self, background_id, message, cwd, model=None):
        self.calls.append(['attach', background_id, message, model])
        agent = next(a for a in self.agents if a['id'] == background_id)
        # Attaching to an ended session starts a new process; a live one keeps its own.
        agent.update(state='working', pid=agent.get('pid') or 2)


class ClaudeApplierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)

    def launch(self, job_id=1):
        return self.launcher.launch(job_id, scope='batch', url='http://127.0.0.1:8790/job1.html',
                                    materials=[self.root / 'materials'], work_dir=self.root / f'work-{job_id}')

    def listings(self):
        return sum(call[1:3] == ['agents', '--json'] for call in self.fake.calls)

    def test_one_listing_serves_every_reconcile_for_a_few_seconds_and_any_change_drops_it(self):
        # 2026-11-19: one 0.4 s listing per launch made a pass over 648 launches take minutes, and no job started.
        from pipeline import claude_applier
        launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach, inventory_ttl=60)
        self.addCleanup(claude_applier._inventories.clear)
        record = launcher.launch(1, scope='batch', url='http://127.0.0.1:8790/job1.html', materials=[self.root / 'materials'], work_dir=self.root / 'work-1')
        claude_applier._inventories.clear()
        before = self.listings()
        for _ in range(5):
            launcher.reconcile(record['launch_id'])
        self.assertEqual(self.listings() - before, 1, 'five reconciles, one listing')
        self.fake.agents[0]['state'] = 'done'
        self.assertEqual(launcher.reconcile(record['launch_id'])['state'], 'running', 'a change outside the launcher waits for the next listing')
        self.assertEqual(launcher.stop(record['launch_id'])['state'], 'stopped', 'its own stop is seen at once')
        self.fake.agents[0]['state'] = 'done'
        launcher.reconcile(record['launch_id'])
        launcher.attach(self.fake.agents[0]['id'], 'hello', str(self.root / 'work-1'))
        self.assertEqual(launcher.reconcile(record['launch_id'])['state'], 'running', 'and so is its own attach')

    def test_an_injected_runner_is_listed_every_time(self):
        record = self.launch()
        before = self.listings()
        self.launcher.reconcile(record['launch_id'])
        self.launcher.reconcile(record['launch_id'])
        self.assertEqual(self.listings() - before, 2)

    def test_a_refused_launch_records_the_reason_and_starts_again_under_the_same_owner(self):
        self.fake.refusal = 'Workspace not trusted. Run `claude` in /x once and accept the trust prompt, then retry.\n'
        record = self.launch()
        self.assertEqual((record['state'], record['background_id']), ('launch_failed', None))
        self.assertEqual([(a['job_id'], a['kind'], a['detail']) for a in self.launcher.store.alerts()],
                         [(1, 'launch_failed', 'Workspace not trusted. Run `claude` in /x once and accept the trust prompt, then retry.')])
        self.assertEqual(self.launcher.retry(record['launch_id'])['state'], 'launch_failed')
        self.fake.refusal = None
        again = self.launch()
        self.assertEqual((again['launch_id'], again['state']), (record['launch_id'], 'running'))
        self.assertEqual(len(self.fake.agents), 1)
        launches = [c for c in self.fake.calls if '--bg' in c]
        self.assertEqual(len(launches), 3)
        self.assertEqual(launches[-1], launches[0])

    def test_an_applier_denies_the_users_own_mcp_servers_and_keeps_its_own(self):
        home = self.root / 'home'
        home.mkdir()
        (home / '.claude.json').write_text(json.dumps({'mcpServers': {'desktop-chromium': {}, 'applier': {}}}))
        with patch('pipeline.claude_applier.Path.home', return_value=home):
            self.launch()
        settings = json.loads((self.root / 'work-1/applier-settings.json').read_text())
        self.assertEqual(settings['deniedMcpServers'], [{'serverName': 'desktop-chromium'}])
        self.assertIn('PreToolUse', settings['hooks'])

    def test_an_unreadable_user_config_denies_nothing(self):
        with patch('pipeline.claude_applier.Path.home', return_value=self.root / 'nobody'):
            self.launch()
        self.assertEqual(json.loads((self.root / 'work-1/applier-settings.json').read_text())['deniedMcpServers'], [])

    def test_retry_leaves_a_live_applier_alone(self):
        record = self.launch()
        calls = len([c for c in self.fake.calls if '--bg' in c])
        self.assertEqual(self.launcher.retry(record['launch_id'])['state'], 'running')
        self.assertEqual(len([c for c in self.fake.calls if '--bg' in c]), calls)

    def test_a_session_still_listed_after_its_process_died_holds_no_slot(self):
        record = self.launch()
        with patch('pipeline.claude_applier.boot_time', return_value=1_000):
            # A session just launched is listed working for a moment before the CLI reports its pid.
            self.fake.agents[0].update(startedAt=2_000_000)
            del self.fake.agents[0]['pid']
            self.assertEqual(self.launcher.reconcile(record['launch_id'])['state'], 'running')
            # After a reboot the CLI keeps listing a dead background session as blocked, with no process.
            self.fake.agents[0].update(state='blocked', startedAt=999_000)
            dead = self.launcher.reconcile(record['launch_id'])
        self.assertEqual(dead['state'], 'finished')
        self.assertFalse(has_process(dead))
        self.assertIsNone(dead['idle_since'])

    def test_launch_freezes_config_and_records_the_session(self):
        record = self.launch()
        self.assertEqual(record['state'], 'running')
        self.assertEqual(record['session_id'], self.fake.agents[0]['sessionId'])
        argv = self.fake.calls[0]
        self.assertEqual(argv[:2], ['claude', '--bg'])
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], ALLOWED_TOOLS)
        self.assertEqual(argv[argv.index('--disallowedTools') + 1], DISALLOWED_TOOLS)
        self.assertIn('AskUserQuestion', argv[argv.index('--disallowedTools') + 1].split())
        self.assertEqual(argv[argv.index('--add-dir') + 1], str((self.root / 'materials').resolve()))
        server = json.loads((self.root / 'work-1' / 'applier-mcp.json').read_text())['mcpServers']['applier']
        self.assertEqual(server['timeout'], MCP_TIMEOUT_MS)
        self.assertEqual(server['args'][-2:], ['--owner', record['launch_id']])

    def test_an_applier_selects_the_recorded_applying_browser(self):
        device = '47419f10-6035-40b3-92fd-3568b80f3c1c'
        self.launch(1)
        (self.root / '.pipeline').mkdir(exist_ok=True)
        (self.root / '.pipeline/apply-browser.json').write_text(json.dumps({'device_id': device}))
        self.launch(2)
        before, after = [c[-1] for c in self.fake.calls if '--bg' in c]
        self.assertIn('the one on this computer', before)
        self.assertIn(f'call select_browser with deviceId {device}, the applying browser', after)
        self.assertNotIn('the one on this computer', after)

    def test_a_second_launch_for_the_job_returns_the_first(self):
        first = self.launch()
        second = self.launch()
        self.assertEqual(first['launch_id'], second['launch_id'])
        self.assertEqual(sum('--bg' in call for call in self.fake.calls), 1)

    def test_unknown_launch_result_is_reconciled_by_name_not_relaunched(self):
        self.fake.launch_output = 'something unexpected\n'
        record = self.launch()
        self.assertEqual(record['state'], 'running')
        self.assertEqual(record['session_id'], self.fake.agents[0]['sessionId'])
        self.launch()
        self.assertEqual(sum('--bg' in call for call in self.fake.calls), 1)

    def test_a_copy_under_the_same_name_is_never_adopted(self):
        record = self.launch()
        copy = dict(self.fake.agents[0], id='deadbeef', sessionId=str(uuid.uuid4()))
        self.fake.agents.append(copy)
        self.assertEqual(self.launcher.reconcile(record['launch_id'])['state'], 'duplicate')

    def test_stop_then_restart_keeps_the_same_session(self):
        record = self.launch()
        with self.assertRaisesRegex(ValueError, 'only a stopped or finished Applier'):
            self.launcher.restart(record['launch_id'])
        self.assertEqual(self.launcher.stop(record['launch_id'])['state'], 'stopped')
        restarted = self.launcher.restart(record['launch_id'])
        self.assertEqual((restarted['state'], restarted['session_id']), ('running', record['session_id']))
        self.assertEqual(self.fake.calls[-2][:2], ['attach', record['background_id']])

    def test_a_finished_session_needs_no_stop_and_can_restart(self):
        record = self.launch()
        self.fake.agents[0].update(state='done', pid=None)
        self.assertEqual(self.launcher.stop(record['launch_id'])['state'], 'finished')
        self.assertFalse(any(call[1] == 'stop' for call in self.fake.calls if call[0] == 'claude'))
        self.assertEqual(self.launcher.restart(record['launch_id'])['state'], 'running')

    def test_a_failed_session_counts_as_ended(self):
        record = self.launch()
        self.fake.agents[0].update(state='failed')
        self.assertEqual(self.launcher.reconcile(record['launch_id'])['state'], 'failed')
        self.assertEqual(self.launcher.restart(record['launch_id'])['state'], 'running')

    def test_an_unexpected_process_change_raises_an_alert_but_a_launcher_restart_does_not(self):
        record = self.launch()
        self.launcher.reconcile(record['launch_id'])
        self.fake.agents[0]['pid'] = 2
        self.launcher.reconcile(record['launch_id'])
        self.assertEqual([a['kind'] for a in self.launcher.store.alerts()], ['process_restarted'])
        self.launcher.stop(record['launch_id'])
        self.fake.attach = lambda bid, msg, cwd, model=None: self.fake.agents[0].update(state='working', pid=3)
        self.launcher.attach = self.fake.attach
        self.launcher.restart(record['launch_id'])
        self.assertEqual(len(self.launcher.store.alerts()), 1)

    def test_drain_stops_waiting_appliers_leaves_working_ones_and_blocks_launches(self):
        waiting, working = self.launch(1), self.launch(2)
        self.launcher.store.ask(1, waiting['launch_id'], 'question', 'Start date?')
        result = self.launcher.drain()
        self.assertEqual(result, {'stopped': [1], 'working': [2], 'ended': []})
        with self.assertRaisesRegex(RuntimeError, 'draining'):
            self.launch(3)
        self.fake.agents[1].update(state='done', pid=None)
        self.assertEqual(self.launcher.drain(), {'stopped': [], 'working': [], 'ended': [1, 2]})
        self.launcher.resume_dispatch()
        self.assertEqual(self.launch(3)['state'], 'running')

    def test_codex_host_variables_never_reach_claude(self):
        env = clean_env({'PATH': '/bin', 'CODEX_THREAD_ID': 't', 'codex_home': 'x', 'HOME': '/h'})
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h'})

    def test_a_parent_claude_code_session_does_not_leak_into_appliers(self):
        env = clean_env({'PATH': '/bin', 'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 's', 'CLAUDE_CODE_CHILD_SESSION': '1',
                         'CLAUDE_CODE_MESSAGING_SOCKET': '/run/x', 'CLAUDE_CODE_MESSAGING_TOKEN': 't', 'CLAUDE_PID': '1',
                         'CLAUDE_CONFIG_DIR': '/h/.claude'})
        self.assertEqual(env, {'PATH': '/bin', 'CLAUDE_CONFIG_DIR': '/h/.claude'})

    def test_wake_restarts_ended_sessions_types_into_idle_ones_and_leaves_busy_ones(self):
        record = self.launch()
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'busy')
        self.fake.agents[0].update(state='blocked')
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'messaged')
        self.fake.agents[0].update(state='working', status='idle')
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'messaged')
        self.fake.agents[0].update(state='done', pid=None)
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'restarted')

    def test_wake_never_restarts_a_stopped_session_or_anything_while_draining(self):
        record = self.launch()
        self.launcher.stop(record['launch_id'])
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'stopped')
        self.fake.agents[0].update(state='done', pid=None)
        self.launcher.store.set_control('draining', 'now')
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'draining')
        self.assertFalse(any(c[0] == 'attach' for c in self.fake.calls))

    def test_a_launch_that_crashed_after_binding_adopts_its_owner(self):
        self.launcher.store.bind(1, 'e' * 32, 'batch')
        record = self.launch()
        self.assertEqual(record['launch_id'], 'e' * 32)
        self.launcher.store.bind(2, 'a' * 32, 'batch')
        with self.assertRaisesRegex(ValueError, 'different Applier owner'):
            self.launcher.launch(2, scope='batch', url='https://x.test', materials=[], work_dir=self.root / 'w2', launch_id='b' * 32)

    def test_the_job_owner_is_the_launch(self):
        record = self.launch()
        with self.assertRaisesRegex(ValueError, 'already has an owner'):
            self.launcher.store.bind(1, 'someone-else', 'batch')
        self.launcher.store.progress(1, record['launch_id'], 'opened the form')

    def prompt(self, job_id):
        return next(c for c in self.fake.calls if '--bg' in c and f'Jobfeed job {job_id}.' in c[-1])[-1]

    def test_with_auto_submit_off_every_answer_the_applier_cannot_look_up_waits_for_the_user(self):
        prompt = (self.launch(), self.prompt(1))[1]
        self.assertIn('never ask a short-answer question without one', prompt)
        self.assertIn('fill nothing in that field until then', prompt)
        self.assertIn('Still ask before typing or drawing a signature.', prompt)
        self.assertNotIn('After you fill an answer you decided', prompt)
        self.assertIn('same_as set to its entry_id', prompt, 'a related answer filled exactly still saves the new wording')

    def test_with_auto_submit_on_the_applier_decides_answers_and_asks_only_when_necessary(self):
        self.launcher.store.set_auto_submit(True, actor='user via test', source='test')
        self.launch(2)
        prompt = self.prompt(2)
        self.assertIn('answer every question yourself and ask only when it is really necessary', prompt)
        self.assertIn('Write each short answer yourself and fill it without asking.', prompt)
        self.assertIn('call record_answer', prompt)
        self.assertIn('is No unless the materials show it', prompt)
        for approval in ('never ask a short-answer question without one', 'always pass draft', 'until the user approves a draft',
                         'Still ask before typing or drawing a signature.'):
            self.assertNotIn(approval, prompt)

    def test_a_posting_that_is_gone_is_reported_closed_in_either_mode(self):
        self.launch(1)
        self.launcher.store.set_auto_submit(True, actor='user via test', source='test')
        self.launch(2)
        for job_id in (1, 2):
            self.assertIn('"Job not found", or a not found (404) page where the posting was, call report_closed', self.prompt(job_id))
            self.assertIn('It is never page_changed, and never a question for the user.', self.prompt(job_id))

    def test_the_applier_closes_tabs_only_when_a_tool_result_says_so_and_then_its_whole_tab_group(self):
        prompt = (self.launch(), self.prompt(1))[1]
        self.assertIn('Close tabs only when a tool result tells you to, and then exactly as it says.', prompt)
        self.assertIn('close every tab in your own tab group as it says, the job listing page and any popup included, and call record_tab_closed', prompt)
        for ending in ('call skip_job with that reply\'s request_id and reply_version, then do what it returns',
                       'call report_closed with the page\'s exact sentence, then do what it returns',
                       'blocker_type captcha and the page URL in page_url, then do what it returns'):
            self.assertIn(ending, prompt)

    def test_stop_ends_a_session_listed_done_that_kept_its_process(self):
        record = self.launch()
        self.fake.agents[0].update(state='done', pid=None)
        self.assertEqual(self.launcher.stop(record['launch_id'])['state'], 'finished')
        self.fake.agents[0].update(state='done', pid=42)
        self.assertEqual(self.launcher.stop(record['launch_id'])['state'], 'stopped')
        self.assertEqual([c[1] for c in self.fake.calls if c[1] == 'stop'], ['stop'])

    def parked(self):
        record = self.launch()
        self.launcher.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.fake.agents[0].update(state='blocked')
        self.assertEqual(self.launcher.park(record['launch_id'])['state'], 'parked')
        return record

    def test_park_keeps_the_session_and_wake_brings_it_back(self):
        record = self.parked()
        self.assertEqual([c for c in self.fake.calls if c[1] == 'stop'], [['claude', 'stop', record['background_id']]])
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'restarted')
        woken = self.launcher.reconcile(record['launch_id'])
        self.assertEqual((woken['state'], woken['session_id']), ('running', record['session_id']))

    def test_stop_turns_a_parked_applier_into_a_stopped_one(self):
        record = self.parked()
        self.assertEqual(self.launcher.stop(record['launch_id'])['state'], 'stopped')
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'stopped')

    def test_drain_lists_a_parked_applier_as_ended(self):
        self.parked()
        self.assertEqual(self.launcher.drain(), {'stopped': [], 'working': [], 'ended': [1]})

    def test_a_parked_session_missing_from_claude_agents_is_reported(self):
        record = self.parked()
        self.fake.agents.clear()
        self.assertEqual(self.launcher.reconcile(record['launch_id'])['state'], 'unknown')
        self.assertEqual([a['kind'] for a in self.launcher.store.alerts()], ['session_missing'])


if __name__ == '__main__':
    unittest.main()


class AttachTests(unittest.TestCase):
    def test_a_message_is_typed_once_the_2_1_319_status_line_shows(self):
        with tempfile.TemporaryDirectory() as temp:
            root, received = Path(temp), Path(temp) / 'received.txt'
            fake = root / 'claude'
            fake.write_text(f'#!{sys.executable}\nimport sys\nprint("❯ \\n⏵⏵ auto mode on · ← 30 agents", flush=True)\n'
                            f'open({str(received)!r}, "w").write(sys.stdin.readline())\n')
            fake.chmod(0o755)
            ClaudeApplierLauncher(root, claude=str(fake))._attach_and_send('abc12345', 'call account_step again', root, ready_timeout=10)
            self.assertEqual(received.read_text().strip(), 'call account_step again')
