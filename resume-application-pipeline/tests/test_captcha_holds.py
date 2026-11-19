"""A CAPTCHA sets its job aside without stalling the batch; synthetic workspace, fake Claude CLI, fake Discord."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_applier_discord import ALLOWED, STRANGER, FakeDiscord, buttons, text
from test_claude_applier import FakeClaude
from tests.runtime_fixture import RuntimeFixture
from pipeline.applier_discord import DiscordBridge
from pipeline.applier_mcp import ApplierTools
from pipeline.captcha_holds import SOLVED_MESSAGE
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.claude_dispatch import ClaudeServices
from pipeline.config import PipelineConfig
from pipeline.lifecycle import ApplicationState as S
from pipeline.orchestrator import ApplicationPipeline
from pipeline.worker import OrchestratorWorker, WorkerSettings

PAGE = 'https://fixture.invalid/jobs/1/apply'


class CaptchaHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = RuntimeFixture(self.temp.name)
        self.config = PipelineConfig.discover(self.f.root)
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.f.root, run=self.fake.run, attach=self.fake.attach)
        patcher = patch('pipeline.claude_dispatch.launcher_for', return_value=self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The Claude background service is up, whatever this computer's own status file says.
        service = patch('pipeline.requeue.stopped_service', return_value=None)
        service.start()
        self.addCleanup(service.stop)
        self.p = ApplicationPipeline(self.config)
        self.p.initialize()
        for job in (1, 2):
            self.p.enqueue(job)
        self.assertEqual(self.p.process_completions(), [1, 2])
        self.services = ClaudeServices(self.p, supervise_every=0, env_path=self.f.root / 'no-discord.env')

    def worker(self, slots=1):
        worker = OrchestratorWorker(self.p, WorkerSettings(application_workers=slots), emit=lambda event: None)
        self.addCleanup(worker.close)
        return worker

    def started(self, worker):
        """Jobs the worker starts in one scheduling pass, once their start has finished."""
        worker._reap()
        before = {job for job, (action, _) in worker._application_futures.items() if action == 'application_started'}
        worker._schedule_applications()
        started = {job: future for job, (action, future) in worker._application_futures.items() if action == 'application_started'}
        for future in started.values():
            future.result(timeout=10)
        worker._reap()
        return sorted(set(started) - before)

    def applier(self, job_id):
        owner = self.p.queue.get(job_id).codex_application_session_id
        return owner, ApplierTools(self.launcher.store, job_id, owner)

    def end_turn(self, owner, state='done'):
        """The session ends its turn. `claude agents` may still list an idle one as blocked."""
        background = self.launcher._record(owner)['background_id']
        next(agent for agent in self.fake.agents if agent['id'] == background)['state'] = state
        return background

    def bridge(self):
        discord = FakeDiscord()
        return discord, DiscordBridge(self.f.root, http=discord, channel_id='general', allowed_user_ids=[ALLOWED], jobfeed_db=self.config.jobfeed_db)

    def cli(self, *args):
        done = subprocess.run([sys.executable, '-m', 'pipeline.cli', '--root', str(self.f.root), 'captcha', *args],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout)

    def test_a_parked_applier_holds_no_slot_so_the_next_job_starts(self):
        batch = self.f.root / '.pipeline/batches/dashboard-claude-parked.json'
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text(json.dumps({'ordered_candidates': [{'id': 1}, {'id': 2}]}))
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, _ = self.applier(1)
        request = self.launcher.store.ask(1, owner, 'question', 'Which team?')
        self.end_turn(owner, 'blocked')
        self.assertEqual(self.launcher.park(owner, request)['state'], 'parked')
        self.assertEqual(self.p.queue.get(1).state, S.APPLYING, 'the job keeps its Applier while it waits')
        self.assertEqual(self.started(worker), [2], 'the parked job holds no slot')

    def test_a_captcha_before_submit_frees_the_slot_and_the_job_waits_queued_until_cleared(self):
        batch = self.f.root / '.pipeline/batches/dashboard-claude-captcha.json'
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text(json.dumps({'application_runtime': 'claude', 'ordered_candidates': [{'id': 1}, {'id': 2}]}))
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        self.assertEqual(self.started(worker), [], 'job 2 waits while job 1 holds the only slot')
        owner, tools = self.applier(1)
        result = tools.report_blocker('captcha', 'Cloudflare asks to verify you are human.', page_url=PAGE)
        self.assertIsNone(result['reply'])
        self.assertIn('a new Applier fills the form again in a fresh tab', result['next'])
        self.assertIn('close every tab in your own tab group', result['next'])
        self.assertEqual(tools.record_tab_closed()['next'], 'End your turn now.')
        self.end_turn(owner)

        self.services.tick(set(worker._application_futures))
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.held), (S.QUEUED, None, True))
        self.assertEqual((item.hold.kind, item.hold.detail, item.hold.page_url), ('captcha', 'Cloudflare asks to verify you are human.', PAGE))
        self.assertIsNone(self.launcher.launch_for(1), 'its Applier is retired')
        self.assertNotIn(1, json.loads(batch.read_text()).get('suspended_job_ids', []), 'it stays in its batch')
        self.assertEqual(self.started(worker), [2], 'the next job starts in the freed slot')

        self.services.tick(set(worker._application_futures))
        self.assertEqual(self.p.process_completions(), [], 'a held job is not prepared again')
        self.assertIsNone(self.p.queue.claim_queued([1]), 'nor claimed by a priority refill')
        self.assertEqual(self.p.queue.get(1).state, S.QUEUED)
        code, listed = self.cli('list')
        self.assertEqual((code, [(row['job_id'], row['state'], row['page_url']) for row in listed]), (0, [(1, 'Queued', PAGE)]))

        code, cleared = self.cli('clear', '1', '2')
        self.assertEqual((code, [(row['job_id'], row['status']) for row in cleared]), (1, [(1, 'cleared'), (2, 'not_held')]))
        self.assertEqual(self.cli('list'), (0, []))
        self.assertEqual(self.p.process_completions(), [1])
        self.assertEqual(self.started(self.worker(slots=2)), [1], 'after the clear it is dispatched again')
        self.assertEqual(sum('--bg' in call for call in self.fake.calls), 3)

    def test_a_captcha_after_a_submit_attempt_keeps_the_session_and_clearing_wakes_it(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, tools = self.applier(1)
        self.launcher.store.authorize_submit(1, source_kind='test', event_id='approval-1')
        self.assertTrue(tools.request_submit()['authorized'])
        result = tools.report_blocker('captcha', 'An hCaptcha appeared after the Submit click.', page_url=PAGE)
        self.assertIn('Leave the tab exactly as it is', result['next'])
        with self.assertRaisesRegex(ValueError, 'only when a tool result tells you to'):
            tools.record_tab_closed()

        self.services.tick(set(worker._application_futures))
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.held), (S.APPLYING, owner, True))
        # A Submit confirmed from the Dashboard or Discord leaves a reservation behind; a held job's reservation takes no slot either.
        reservation = [{'job': 1, 'state': 'queued', 'prompt': 'Submit confirmed.', 'terminal_turn': False}]
        with patch('pipeline.submission_reservation.reservations', return_value=reservation):
            self.assertEqual(self.started(worker), [2], 'only the slot is released, even before the session ends its turn')
        self.end_turn(owner)
        self.services.tick(set(worker._application_futures))
        self.assertEqual(self.launcher.launch_for(1), owner)
        self.assertEqual(self.launcher.store.requeues(1), [])
        self.assertFalse(any(call[1:2] == ['stop'] for call in self.fake.calls), 'the session and its tab stay as they are')
        self.assertEqual([alert['kind'] for alert in self.launcher.store.alerts() if alert['job_id'] == 1], [])

        discord = FakeDiscord()
        bridge = DiscordBridge(self.f.root, http=discord, channel_id='general', allowed_user_ids=[ALLOWED], jobfeed_db=self.config.jobfeed_db)
        bridge.sync()
        bridge.sync()
        thread = bridge.thread_for(1)
        [notice] = [m for m in discord.messages[thread] if m['author']['bot']]
        for words in ('set aside as CAPTCHA needed', 'batch goes on with other jobs', 'No Applier tries to solve it', PAGE):
            self.assertIn(words, text(notice))
        self.assertEqual(list(buttons(notice)), ['I solved it, retry'])

        def press(user):
            bridge.interact({'id': discord.new_id(), 'token': 'tok', 'application_id': 'app', 'type': 3, 'message': notice,
                             'data': {'custom_id': buttons(notice)['I solved it, retry']}, 'member': {'user': {'id': user}}})
            return discord.callbacks[-1]
        self.assertIn('Only the account', press(STRANGER)['data']['content'])
        self.assertTrue(self.p.queue.get(1).held)
        self.assertIn('looks at its tab again', press(ALLOWED)['data']['content'])
        self.assertEqual(self.p.queue.get(1).hold.cleared_by, f'discord:{ALLOWED}')

        self.services.tick(set(worker._application_futures))
        self.assertIn(['attach', self.launcher._record(owner)['background_id'], SOLVED_MESSAGE, None], self.fake.calls)
        self.assertEqual(self.launcher.reconcile(owner)['state'], 'running')
        self.assertIn(owner, worker._active_application_threads(), 'the woken Applier takes a slot again')
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.held), (S.APPLYING, owner, False))
        self.assertEqual(sum('--bg' in call for call in self.fake.calls), 2, 'it is never filled again by a new Applier')
        self.assertEqual(self.launcher.store.requeues(1), [])

    def test_an_applier_still_ending_its_turn_is_never_stopped_and_goes_back_once_it_ends(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, tools = self.applier(1)
        tools.report_blocker('captcha', 'reCAPTCHA asks to pick the traffic lights.', page_url=PAGE)
        events = self.services.tick(set(worker._application_futures))
        self.assertIn('captcha_requeue_refused', [event['event'] for event in events])
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.held), (S.APPLYING, owner, True))
        self.assertFalse(any(call[1:2] == ['stop'] for call in self.fake.calls))
        self.assertEqual(self.started(worker), [2])
        self.end_turn(owner)
        self.services.tick(set(worker._application_futures))
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.held), (S.QUEUED, None, True))

    def test_a_tab_lost_before_submit_sends_the_job_back_to_be_filled_again_and_tells_the_user_once(self):
        batch = self.f.root / '.pipeline/batches/dashboard-claude-lost-tab.json'
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text(json.dumps({'application_runtime': 'claude', 'ordered_candidates': [{'id': 1}, {'id': 2}]}))
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, tools = self.applier(1)
        tools.register_tab(41, PAGE)
        # Job 742: Submit was confirmed, and the woken Applier found its tab gone before it called request_submit.
        self.launcher.store.authorize_submit(1, source_kind='test', event_id='approval-1')
        result = tools.report_blocker('tab_missing', 'My form tab is gone.')
        self.assertIsNone(result['reply'])
        self.assertIn('End your turn now', result['next'])

        events = self.services.tick(set(worker._application_futures))
        self.assertIn('lost_tab_requeue_refused', [event['event'] for event in events])
        self.assertEqual((self.p.queue.get(1).state, self.launcher.launch_for(1)), (S.APPLYING, owner), 'a working session is left for a later pass')
        self.assertFalse(any(call[1:2] == ['stop'] for call in self.fake.calls))
        discord, bridge = self.bridge()
        bridge.sync()
        self.assertNotIn('Blocked', json.dumps(discord.messages.get('general', [])), 'the user is not asked to reopen or stop')
        self.assertEqual(discord.messages.get(bridge.thread_for(1), []), [], 'the user is not asked to reopen or stop')

        self.end_turn(owner)
        events = self.services.tick(set(worker._application_futures))
        self.assertIn({'event': 'lost_tab_requeued', 'job_id': 1}, events)
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.hold), (S.QUEUED, None, None))
        self.assertIsNone(self.launcher.launch_for(1), 'its Applier is retired')
        self.assertFalse(self.launcher.store.unused_submit_auth(1), 'the approval it never spent is withdrawn')
        self.assertNotIn(1, json.loads(batch.read_text()).get('suspended_job_ids', []), 'it stays in its batch')
        self.assertEqual(self.started(worker), [2], 'the next job starts in the freed slot')
        self.assertEqual([event for event in self.services.tick(set(worker._application_futures)) if event.get('job_id') == 1], [])
        self.assertEqual(len(self.launcher.store.requeues(1)), 1)

        bridge.sync()
        bridge.sync()
        [notice] = [m for m in discord.messages[bridge.thread_for(1)] if m['author']['bot']]
        for words in ('form tab was lost', 'nothing was sent', 'went back to the queue', 'fills the form again'):
            self.assertIn(words, text(notice))
        self.assertEqual(list(buttons(notice)), [])

        self.assertEqual(self.p.process_completions(), [1])
        self.assertEqual(self.started(self.worker(slots=2)), [1], 'a new Applier fills it again in its normal turn')
        self.assertEqual(sum('--bg' in call for call in self.fake.calls), 3)

    def test_a_tab_lost_after_a_submit_attempt_keeps_the_job_and_asks_the_user(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, tools = self.applier(1)
        tools.register_tab(41, PAGE)
        self.launcher.store.authorize_submit(1, source_kind='test', event_id='approval-1')
        self.assertTrue(tools.request_submit()['authorized'])
        waiting = threading.Event()
        waiting.set()
        self.assertNotIn('End your turn', tools.report_blocker('tab_missing', 'My form tab is gone after the click.', stop=waiting)['next'])
        self.end_turn(owner, 'blocked')
        self.assertEqual(self.services.tick(set(worker._application_futures)), [])
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id), (S.APPLYING, owner))
        self.assertEqual((self.launcher.launch_for(1), self.launcher.store.requeues(1)), (owner, []))
        self.assertFalse(any(call[1:2] == ['stop'] for call in self.fake.calls), 'the session and its tab stay as they are')
        discord, bridge = self.bridge()
        bridge.sync()
        self.assertIn('Blocked: tab missing', json.dumps(discord.messages['general']))

    def test_an_idle_session_still_listed_blocked_is_stopped_then_requeued(self):
        worker = self.worker(slots=2)
        self.assertEqual(self.started(worker), [1, 2])
        sessions = {}
        for job, blocker in ((1, 'captcha'), (2, 'tab_missing')):
            owner, tools = self.applier(job)
            tools.register_tab(40 + job, PAGE)
            tools.report_blocker(blocker, 'The Applier cannot go on.', page_url=PAGE)
            sessions[job] = self.end_turn(owner, 'blocked')
        events = self.services.tick(set(worker._application_futures))
        for job, kind in ((1, 'captcha'), (2, 'lost_tab')):
            self.assertIn({'event': f'{kind}_requeued', 'job_id': job}, events)
            self.assertEqual((self.p.queue.get(job).state, self.p.queue.get(job).held), (S.QUEUED, job == 1))
            self.assertIn(['claude', 'stop', sessions[job]], self.fake.calls)

    def test_other_blockers_keep_the_job_and_its_slot_as_before(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, tools = self.applier(1)
        waiting = threading.Event()
        waiting.set()
        tools.report_blocker('user_only_check', 'The site asks for a code sent to your phone.', stop=waiting)
        self.services.tick(set(worker._application_futures))
        item = self.p.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.hold), (S.APPLYING, owner, None))
        self.assertEqual(self.started(worker), [])
        self.assertEqual(self.launcher.launch_for(1), owner)

    def test_an_applier_idle_on_an_unanswered_request_frees_its_slot_and_keeps_its_session(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, _ = self.applier(1)
        self.launcher.store.ask(1, owner, 'blocker', 'Click the consent radio button in the open tab.', blocker_type='user_only_check')
        self.assertEqual(self.started(worker), [], 'a session still working keeps its slot')
        self.end_turn(owner, 'blocked')
        self.assertEqual(self.started(worker), [2], 'an Applier idle on the user frees its slot')
        self.assertIn(owner, worker._active_application_threads())
        self.assertEqual((self.p.queue.get(1).state, self.launcher.launch_for(1)), (S.APPLYING, owner))
        self.assertFalse(any(call[1:2] == ['stop'] for call in self.fake.calls), 'the session and its tab stay as they are')

    def test_an_idle_applier_keeps_its_slot_while_its_request_is_answered_or_escalated(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, _ = self.applier(1)
        question = self.launcher.store.ask(1, owner, 'question', 'Share your resume with partners?')
        self.launcher.store.reply(question, 'Yes', source_kind='discord', source_id='m1', sender_id=ALLOWED)
        self.launcher.store.ask(1, owner, 'blocker', 'The resume upload is stuck.', field='Resume', blocker_type='broken_component',
                                evidence={'attempts': ['form_input', 'computer', 'javascript_tool']})
        self.end_turn(owner, 'blocked')
        self.assertEqual(self.started(worker), [], 'an escalation waits on the supervisor, not the user')

    def test_a_submit_reservation_holds_the_slot_of_an_applier_idle_on_the_user(self):
        worker = self.worker()
        self.assertEqual(self.started(worker), [1])
        owner, _ = self.applier(1)
        self.launcher.store.ask(1, owner, 'blocker', 'Did the Submit click go through?', blocker_type='submit_unknown')
        self.end_turn(owner, 'blocked')
        reservation = [{'job': 1, 'state': 'queued', 'prompt': 'Submit confirmed.', 'terminal_turn': False}]
        with patch('pipeline.submission_reservation.reservations', return_value=reservation):
            self.assertEqual(self.started(worker), [])
        self.assertEqual(self.started(worker), [2])


if __name__ == '__main__':
    unittest.main()
