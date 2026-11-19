from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest

from pipeline.applier_mcp import ESCALATED_INSTRUCTION, SKIPPED_INSTRUCTION, ApplierTools
from pipeline.applier_supervisor import tick
from pipeline.claude_applier import RESTART_MESSAGE, ClaudeApplierLauncher
from test_claude_applier import FakeClaude

ATTEMPTS = ['form_input', 'computer', 'javascript_tool']


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        self.store = self.launcher.store

    def launch(self, job_id=1, url='http://127.0.0.1:8790/widgets.html'):
        record = self.launcher.launch(job_id, scope='batch', url=url, materials=[], work_dir=self.root / f'work-{job_id}')
        return record, ApplierTools(self.store, job_id, record['launch_id'])

    def agent(self, index=0):
        return self.fake.agents[index]

    def test_first_broken_component_escalates_to_the_strong_model_once(self):
        record, tools = self.launch()
        result = tools.report_blocker('broken_component', 'Location picker is stuck', field='location', attempts=ATTEMPTS)
        self.assertEqual(result['next'], ESCALATED_INSTRUCTION)
        self.assertEqual(tick(self.launcher), [])
        self.agent().update(state='done', pid=None)
        events = tick(self.launcher)
        self.assertIn({'event': 'escalated', 'job_id': 1, 'field': 'location'}, events)
        attach = next(call for call in self.fake.calls if call[0] == 'attach')
        self.assertEqual(attach[3], 'opus')
        self.assertIn('form_input, computer, javascript_tool', attach[2])
        self.assertEqual(self.store.jobs()[0]['model'], 'opus')
        self.assertEqual(self.store.request(result['request_id'])['state'], 'escalation_sent')
        second = self.store.ask(1, record['launch_id'], 'blocker', 'Location picker is stuck', field='location',
                                blocker_type='broken_component', evidence={'attempts': ATTEMPTS})
        self.assertEqual(self.store.request(second)['state'], 'open')

    def test_an_applier_left_idle_after_its_escalation_is_stopped_and_restarted_on_the_strong_model(self):
        record, tools = self.launch()
        result = tools.report_blocker('broken_component', 'Location picker is stuck', field='location', attempts=ATTEMPTS)
        # Told to end its turn, the session may stay listed as blocked instead of done.
        self.agent().update(state='blocked')
        events = tick(self.launcher)
        self.assertIn({'event': 'escalated', 'job_id': 1, 'field': 'location'}, events)
        self.assertIn(['stop', record['background_id']], [call[1:3] for call in self.fake.calls])
        attach = next(call for call in self.fake.calls if call[0] == 'attach')
        self.assertEqual(attach[3], 'opus')
        self.assertEqual(self.store.request(result['request_id'])['state'], 'escalation_sent')

    def test_a_deliberately_stopped_applier_is_not_escalated(self):
        record, tools = self.launch()
        tools.report_blocker('broken_component', 'Location picker is stuck', field='location', attempts=ATTEMPTS)
        self.launcher.stop(record['launch_id'])
        events = tick(self.launcher)
        self.assertFalse(any(e['event'] == 'escalated' for e in events))
        self.assertFalse(any(call[0] == 'attach' for call in self.fake.calls))
        self.assertEqual([e['detail'] for e in events if e['event'] == 'alert'], ['stopped after: no progress'])

    def test_a_hard_site_starts_on_the_strong_model_and_blocks_straight_to_the_user(self):
        record, tools = self.launch(url='https://acme.wd1.myworkdayjobs.com/jobs/1')
        argv = self.fake.calls[0]
        self.assertEqual(argv[argv.index('--model') + 1], 'opus')
        request = self.store.ask(1, record['launch_id'], 'blocker', 'Date picker is stuck', field='start',
                                 blocker_type='broken_component', evidence={'attempts': ATTEMPTS})
        self.assertEqual(self.store.request(request)['state'], 'open')

    def test_a_stall_is_reported_once_and_a_wait_is_not_a_stall(self):
        record, tools = self.launch()
        tools.record_progress('filled name')
        later = datetime.now(timezone.utc) + timedelta(minutes=11)
        events = tick(self.launcher, now=later)
        self.assertEqual([e['kind'] for e in events if e['event'] == 'alert'], ['stalled'])
        self.assertEqual(tick(self.launcher, now=later), [])
        self.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.assertEqual(tick(self.launcher, now=later + timedelta(minutes=30)), [])

    def test_a_skipped_job_is_never_reported_stalled(self):
        record, tools = self.launch()
        tools.record_progress('filled name')
        self.store.ignore(1, source_id='m1', sender_id='400')
        later = datetime.now(timezone.utc) + timedelta(minutes=11)
        self.assertNotIn('stalled', [e.get('kind') for e in tick(self.launcher, now=later)])

    def test_a_session_that_stops_before_review_is_reported(self):
        record, tools = self.launch()
        tools.record_progress('filled name')
        self.agent().update(state='done', pid=None)
        events = tick(self.launcher)
        self.assertEqual([(e['kind'], e['detail']) for e in events], [('stopped_unfinished', 'finished after: filled name')])
        tools.record_progress('ready for review')
        self.assertEqual(tick(self.launcher), [])

    def test_a_session_stopped_while_asking_is_reported(self):
        record, tools = self.launch()
        self.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.launcher.stop(record['launch_id'])
        events = tick(self.launcher)
        self.assertEqual([e['detail'] for e in events if e['event'] == 'alert'], ['stopped while waiting on the user: no progress'])

    def test_a_submitted_session_that_ends_is_not_an_alert(self):
        record, tools = self.launch()
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        tools.record_submit_outcome('submitted', 'Thank you for applying')
        self.agent().update(state='done', pid=None)
        self.assertEqual([e for e in tick(self.launcher) if e.get('kind') == 'stopped_unfinished'], [])

    def test_a_skip_the_user_asked_for_runs_once_and_is_not_an_alert(self):
        record, tools = self.launch()
        request = self.store.ask(1, record['launch_id'], 'question', 'The posting is gone. What now?')
        with self.assertRaisesRegex(ValueError, 'user reply'):
            tools.skip_job(request, 1)
        self.store.reply(request, 'Skip this posting and mark it ignored', source_kind='discord', source_id='m1', sender_id='400')
        self.assertEqual(tools.skip_job(request, 1)['skipped'], True)
        self.agent().update(state='done', pid=None)
        skipped, synced = [], []
        events = tick(self.launcher, sync=synced.append, skip=skipped.append)
        self.assertIn({'event': 'user_skip', 'job_id': 1}, events)
        self.assertEqual((skipped, synced), ([1], []))
        self.assertEqual([e for e in events if e.get('kind') == 'stopped_unfinished'], [])
        tick(self.launcher, sync=synced.append, skip=skipped.append)
        self.assertEqual(skipped, [1])
        self.assertEqual(self.store.skip(1)['reason'], 'Skip this posting and mark it ignored')

    def test_a_closed_posting_runs_its_own_handler_once_and_is_not_an_alert(self):
        record, tools = self.launch()
        with self.assertRaisesRegex(ValueError, 'ask the user instead'):
            tools.report_closed('The page shows a login wall')
        self.assertIn('close every tab in your own tab group', tools.report_closed('This job has expired on Indeed')['next'])
        self.agent().update(state='done', pid=None)
        closed, skipped = [], []
        events = tick(self.launcher, skip=skipped.append, closed=closed.append)
        self.assertIn({'event': 'posting_closed', 'job_id': 1}, events)
        self.assertEqual((closed, skipped), ([1], []))
        self.assertEqual([e for e in events if e.get('kind') == 'stopped_unfinished'], [])

    def test_after_a_verified_submission_the_applier_records_its_closed_tab(self):
        record, tools = self.launch()
        tools.register_tab(5, 'http://127.0.0.1:8790/widgets.html')
        with self.assertRaisesRegex(ValueError, 'only when a tool result tells you to'):
            tools.record_tab_closed()
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        with self.assertRaisesRegex(ValueError, 'only when a tool result tells you to'):
            tools.record_tab_closed()
        self.assertIn('close every tab in your own tab group', tools.record_submit_outcome('submitted', 'Thanks for applying')['next'])
        self.assertEqual(tools.record_tab_closed(), {'closed': ['5'], 'next': 'Call record_progress with "submitted" and end with a one-line summary.'})
        self.assertTrue(self.store.tabs_closed(1))

    def test_an_unknown_outcome_keeps_the_tab_and_the_session_for_the_user(self):
        record, tools = self.launch()
        tools.register_tab(5, 'http://127.0.0.1:8790/widgets.html')
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        self.assertNotIn('close', tools.record_submit_outcome('unknown', 'The page went blank after the click')['next'])
        with self.assertRaisesRegex(ValueError, 'only when a tool result tells you to'):
            tools.record_tab_closed()
        self.agent().update(state='blocked')
        tick(self.launcher, sync=lambda job_id: None)
        self.assertNotIn('stop', [call[1] for call in self.fake.calls])

    def submitted(self, tools):
        tools.register_tab(5, 'http://127.0.0.1:8790/widgets.html')
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        tools.record_submit_outcome('submitted', 'Thanks for applying')

    def stops(self):
        return [call[1:3] for call in self.fake.calls if call[1] == 'stop']

    def test_a_submitted_job_keeps_its_working_session_until_it_ends_its_turn_and_then_stops_it(self):
        record, tools = self.launch()
        self.submitted(tools)
        synced = []
        events = tick(self.launcher, sync=synced.append)
        self.assertEqual((synced, self.stops()), ([1], []), 'the Applier may still be closing its tabs')
        self.assertNotIn('finished_session_stopped', [e['event'] for e in events])
        tools.record_tab_closed()
        self.agent().update(state='blocked')
        self.assertIn({'event': 'finished_session_stopped', 'job_id': 1}, tick(self.launcher, sync=synced.append))
        self.assertEqual(self.stops(), [['stop', record['background_id']]])
        self.assertEqual(tick(self.launcher, sync=synced.append), [])
        self.assertEqual(len(self.stops()), 1)

    def test_a_finished_session_still_working_after_the_grace_is_stopped(self):
        record, tools = self.launch()
        self.submitted(tools)
        tick(self.launcher, sync=lambda job_id: None)
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        self.assertIn({'event': 'finished_session_stopped', 'job_id': 1}, tick(self.launcher, now=later, sync=lambda job_id: None))

    def test_a_finished_session_listed_done_with_a_live_process_is_stopped(self):
        # Job 765 in the live store: skipped, listed done, and its process still held about 300 MB a day and a half later.
        record, tools = self.launch()
        self.submitted(tools)
        self.agent().update(state='done', pid=42)
        self.assertIn({'event': 'finished_session_stopped', 'job_id': 1}, tick(self.launcher, sync=lambda job_id: None))
        self.assertEqual(self.stops(), [['stop', record['background_id']]])

    def test_a_finished_session_whose_process_exited_is_left_alone(self):
        record, tools = self.launch()
        self.submitted(tools)
        self.agent().update(state='done', pid=None)
        self.assertEqual(tick(self.launcher, sync=lambda job_id: None), [{'event': 'synced', 'job_id': 1}])
        self.assertEqual(self.stops(), [])

    def test_a_submitted_job_whose_sync_failed_keeps_its_session(self):
        record, tools = self.launch()
        self.submitted(tools)
        self.agent().update(state='blocked')

        def fail(job_id):
            raise RuntimeError('Jobfeed is locked')
        tick(self.launcher, sync=fail)
        self.assertEqual(self.stops(), [])

    def test_a_skipped_or_closed_job_closes_its_tab_group_and_its_session_stops(self):
        for job_id, end in ((1, 'skip'), (2, 'closed')):
            record, tools = self.launch(job_id)
            tools.register_tab(job_id * 10, 'http://127.0.0.1:8790/widgets.html')
            if end == 'skip':
                request = self.store.ask(job_id, record['launch_id'], 'question', 'The posting looks stale. What now?')
                self.store.reply(request, 'Skip this posting', source_kind='discord', source_id=f'm{job_id}', sender_id='400')
                result = tools.skip_job(request, 1)
            else:
                result = tools.report_closed('This job has expired on Indeed')
            self.assertIn('close every tab in your own tab group', result['next'])
            self.assertEqual(tools.record_tab_closed(), {'closed': [str(job_id * 10)], 'next': 'End your turn now.'})
            self.agent(job_id - 1).update(state='blocked')
        events = tick(self.launcher, skip=lambda job_id: None, closed=lambda job_id: None)
        self.assertEqual({e['job_id'] for e in events if e['event'] == 'finished_session_stopped'}, {1, 2})

    def test_an_ignored_job_ends_the_wait_and_its_applier_closes_its_tabs_and_never_submits(self):
        record, tools = self.launch()
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        results = []
        waiting = threading.Thread(target=lambda: results.append(tools.ask_user('Which team?', field='team', suggestions=['Platform', 'Payments'])))
        waiting.start()
        while not self.store.requests():
            time.sleep(0.01)
        self.store.ignore(1, source_id='interaction-1', sender_id='400')
        waiting.join(5)
        self.assertEqual(results[0]['next'], SKIPPED_INSTRUCTION)
        self.assertEqual(tools.request_submit(), {'authorized': False, 'next': SKIPPED_INSTRUCTION})
        self.assertTrue(self.store.unused_submit_auth(1))
        self.assertEqual(tools.ask_user('Anything else?'), {'reply': None, 'next': SKIPPED_INSTRUCTION})
        self.assertEqual(len(self.store.requests()), 1, 'a skipped job asks nothing more')
        self.assertEqual(tools.record_tab_closed()['next'], 'End your turn now.')

    def test_a_job_with_a_requested_submit_cannot_be_skipped(self):
        record, tools = self.launch()
        request = self.store.ask(1, record['launch_id'], 'question', 'Anything else?')
        self.store.reply(request, 'skip it', source_kind='discord', source_id='m1', sender_id='400')
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        with self.assertRaisesRegex(ValueError, 'cannot be skipped'):
            tools.skip_job(request, 1)

    def test_a_queued_message_waits_until_the_applier_is_idle(self):
        record, tools = self.launch()
        self.store.queue_message(1, 'The user confirmed Submit.', 'm1')
        self.store.queue_message(1, 'The user confirmed Submit.', 'm1')
        self.assertEqual([e for e in tick(self.launcher) if e['event'] == 'message_delivered'], [])
        self.agent().update(state='blocked')
        events = [e for e in tick(self.launcher) if e['event'] == 'message_delivered']
        self.assertEqual(events, [{'event': 'message_delivered', 'job_id': 1, 'outcome': 'messaged'}])
        self.assertEqual(self.store.pending_messages(), [])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == 'attach']), 1)

    def test_a_claimed_message_is_never_delivered_twice(self):
        record, tools = self.launch()
        self.store.queue_message(1, 'go', 'm2')
        self.assertTrue(self.store.claim_message('m2'))
        self.agent().update(state='blocked')
        self.assertEqual([e for e in tick(self.launcher) if e['event'] == 'message_delivered'], [])
        self.assertFalse(any(c[0] == 'attach' for c in self.fake.calls))

    def test_a_message_whose_delivery_never_finished_is_reported_once_and_never_resent(self):
        record, tools = self.launch()
        self.store.queue_message(1, 'The user confirmed Submit in Discord for job 1.', 'm3')
        self.assertTrue(self.store.claim_message('m3'))
        self.agent().update(state='blocked')
        unconfirmed = lambda events: [e for e in events if e.get('kind') == 'message_unconfirmed']
        self.assertEqual(unconfirmed(tick(self.launcher)), [])
        later = datetime.now(timezone.utc) + timedelta(minutes=11)
        events = unconfirmed(tick(self.launcher, now=later))
        self.assertEqual([e['job_id'] for e in events], [1])
        self.assertIn('The user confirmed Submit in Discord for job 1.', events[0]['detail'])
        self.assertEqual(unconfirmed(tick(self.launcher, now=later)), [])
        self.assertFalse(any(c[0] == 'attach' for c in self.fake.calls))

    def test_a_wake_that_fails_on_the_inventory_leaves_its_message_queued(self):
        record, tools = self.launch()
        self.store.queue_message(1, 'go', 'm4')
        run = self.launcher.run

        def timed_out(argv, **kwargs):
            if argv[1:2] == ['agents']:
                raise subprocess.TimeoutExpired(argv, 60)
            return run(argv, **kwargs)
        self.launcher.run = timed_out
        with self.assertRaises(Exception):
            tick(self.launcher)
        self.assertEqual([m['id'] for m in self.store.pending_messages()], ['m4'])

    def test_a_field_read_back_three_times_is_a_loop(self):
        record, tools = self.launch()
        for _ in range(3):
            tools.record_readback('location', '')
        events = tick(self.launcher)
        self.assertIn({'event': 'alert', 'job_id': 1, 'kind': 'readback_loop', 'detail': 'location'}, events)

    def attaches(self):
        return [call for call in self.fake.calls if call[0] == 'attach']

    def state(self, record):
        return self.launcher.reconcile(record['launch_id'])['state']

    def answer(self, request):
        self.store.reply(request, 'June 1', source_kind='discord', source_id=f'reply-{request}', sender_id='400')

    def idle_for(self, record, minutes):
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE applier_launches SET idle_since=? WHERE launch_id=?', (since, record['launch_id']))

    def park_on_a_question(self):
        record, tools = self.launch()
        request = self.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.agent().update(state='blocked')
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        events = tick(self.launcher, now=now + timedelta(minutes=16))
        self.assertIn({'event': 'idle_session_parked', 'job_id': 1, 'awaiting': request}, events)
        return record, tools, request, now

    def test_an_applier_waiting_on_the_user_is_parked_after_the_threshold_and_resumes_with_the_saved_reply(self):
        record, tools = self.launch()
        request = self.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.agent().update(state='blocked')
        now = datetime.now(timezone.utc)
        self.assertEqual(tick(self.launcher, now=now), [])
        self.assertEqual(tick(self.launcher, now=now + timedelta(minutes=14)), [])
        self.assertEqual(self.stops(), [])
        events = tick(self.launcher, now=now + timedelta(minutes=16))
        self.assertEqual(self.stops(), [['stop', record['background_id']]], 'an Applier idle on a question past PARK_AFTER keeps its process')
        self.assertEqual(events, [{'event': 'idle_session_parked', 'job_id': 1, 'awaiting': request}])
        self.assertEqual(self.state(record), 'parked')
        self.answer(request)
        self.assertEqual(tick(self.launcher), [{'event': 'parked_session_resumed', 'job_id': 1}])
        self.assertEqual(self.attaches(), [['attach', record['background_id'], RESTART_MESSAGE, None]])
        resumed = self.launcher.reconcile(record['launch_id'])
        self.assertEqual((resumed['state'], resumed['session_id']), ('running', record['session_id']))
        self.assertEqual(tools.ask_user('Start date?', suggestions=['June 1', 'July 1'])['reply'], 'June 1')

    def test_answering_many_parked_appliers_restarts_only_a_few_at_a_time(self):
        from pipeline.applier_supervisor import RESUME_AT_ONCE
        now, requests, total = datetime.now(timezone.utc), [], RESUME_AT_ONCE + 2
        for job_id in range(1, total + 1):
            record, _ = self.launch(job_id)
            requests.append(self.store.ask(job_id, record['launch_id'], 'question', 'Start date?'))
            self.agent(job_id - 1).update(state='blocked')
        tick(self.launcher, now=now)
        self.assertEqual(len(tick(self.launcher, now=now + timedelta(minutes=16))), total, 'all of them park')
        for request in requests:
            self.answer(request)
        resumed = [e['job_id'] for e in tick(self.launcher) if e['event'] == 'parked_session_resumed']
        self.assertEqual(len(resumed), RESUME_AT_ONCE)
        self.assertEqual([e for e in tick(self.launcher) if e['event'] == 'parked_session_resumed'], [], 'the rest wait while those work')
        for job_id in resumed:
            self.agent(job_id - 1).update(state='blocked')
        later = [e['job_id'] for e in tick(self.launcher) if e['event'] == 'parked_session_resumed']
        self.assertEqual(sorted(resumed + later), list(range(1, total + 1)), 'and come back once the first ones go idle')

    def test_a_parked_applier_raises_no_alert(self):
        record, tools, request, now = self.park_on_a_question()
        self.assertEqual([e for e in tick(self.launcher, now=now + timedelta(minutes=60)) if e['event'] == 'alert'], [])

    def test_a_job_with_a_submit_attempt_keeps_its_idle_session(self):
        record, tools = self.launch()
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        tools.request_submit()
        tools.record_submit_outcome('unknown', 'The page went blank after the click')
        self.agent().update(state='blocked')
        waiting = self.waiting_control(2)
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        tick(self.launcher, now=now + timedelta(minutes=16))
        self.assertEqual(self.stops(), [['stop', waiting['background_id']]])

    def waiting_control(self, job_id):
        record, tools = self.launch(job_id)
        self.store.ask(job_id, record['launch_id'], 'question', 'Start date?')
        self.agent(job_id - 1).update(state='blocked')
        return record

    def test_captcha_lost_tab_and_escalation_jobs_keep_their_sessions(self):
        for job_id, blocker_type in ((1, 'captcha'), (2, 'tab_missing')):
            record, tools = self.launch(job_id)
            self.store.ask(job_id, record['launch_id'], 'blocker', f'{blocker_type} on the form', blocker_type=blocker_type)
            self.agent(job_id - 1).update(state='blocked')
        record, tools = self.launch(3)
        tools.report_blocker('broken_component', 'Location picker is stuck', field='location', attempts=ATTEMPTS)
        self.agent(2).update(state='failed')
        waiting = self.waiting_control(4)
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        tick(self.launcher, now=now + timedelta(minutes=16))
        self.assertEqual(self.stops(), [['stop', waiting['background_id']]])

    def test_an_applier_idle_at_review_is_parked_and_a_queued_message_restarts_it(self):
        record, tools = self.launch()
        tools.record_progress('ready for review')
        self.agent().update(state='done')
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        self.assertEqual(tick(self.launcher, now=now + timedelta(minutes=16)), [{'event': 'idle_session_parked', 'job_id': 1, 'awaiting': None}])
        self.assertEqual(tick(self.launcher, now=now + timedelta(minutes=30)), [])
        self.assertEqual(self.attaches(), [])
        self.store.queue_message(1, 'The user confirmed Submit.', 'm1')
        self.assertEqual(tick(self.launcher), [{'event': 'message_delivered', 'job_id': 1, 'outcome': 'restarted'}])
        self.assertEqual(self.attaches(), [['attach', record['background_id'], 'The user confirmed Submit.', None]])

    def test_a_message_that_wakes_an_applier_parked_on_a_question_tells_it_to_ask_again(self):
        record, tools, request, now = self.park_on_a_question()
        self.store.queue_message(1, 'Make the draft shorter.', 'm1')
        self.assertEqual(tick(self.launcher), [{'event': 'message_delivered', 'job_id': 1, 'outcome': 'restarted'}])
        self.assertEqual(self.attaches(), [['attach', record['background_id'], f'{RESTART_MESSAGE}\n\nMake the draft shorter.', None]])

    def test_a_parked_applier_stopped_on_purpose_never_comes_back(self):
        record, tools, request, now = self.park_on_a_question()
        self.launcher.stop(record['launch_id'])
        self.answer(request)
        tick(self.launcher)
        self.assertEqual((self.attaches(), self.state(record)), ([], 'stopped'))

    def test_nothing_parked_comes_back_while_draining(self):
        record, tools, request, now = self.park_on_a_question()
        self.store.set_control('draining', 'now')
        self.answer(request)
        self.assertEqual(tick(self.launcher), [])
        self.assertEqual((self.attaches(), self.state(record)), ([], 'parked'))
        self.launcher.resume_dispatch()
        self.assertEqual(tick(self.launcher), [{'event': 'parked_session_resumed', 'job_id': 1}])

    def test_a_pass_that_dies_before_claude_stop_parks_on_the_next_pass(self):
        record, tools = self.launch()
        self.store.ask(1, record['launch_id'], 'question', 'Start date?')
        self.agent().update(state='blocked')
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        run = self.launcher.run

        def stop_times_out(argv, **kwargs):
            if argv[1] == 'stop':
                raise subprocess.TimeoutExpired(argv, 60)
            return run(argv, **kwargs)
        self.launcher.run = stop_times_out
        with self.assertRaises(subprocess.TimeoutExpired):
            tick(self.launcher, now=now + timedelta(minutes=16))
        self.launcher.run = run
        self.assertEqual(self.state(record), 'running')
        self.assertEqual(self.launcher.wake(record['launch_id'], 'go'), 'messaged')
        self.agent().update(state='blocked')
        self.assertIn('idle_session_parked', [e['event'] for e in tick(self.launcher, now=now + timedelta(minutes=17))])
        self.assertEqual(self.state(record), 'parked')

    def test_a_message_typed_into_an_idle_applier_restarts_its_idle_clock(self):
        self.launcher.attach = lambda background_id, message, cwd, model=None: self.fake.calls.append(['attach', background_id, message, model])
        records = {}
        for job_id in (1, 2):
            records[job_id], tools = self.launch(job_id)
            self.store.ask(job_id, records[job_id]['launch_id'], 'question', 'Start date?')
            self.agent(job_id - 1).update(state='blocked')
        tick(self.launcher)
        for record in records.values():
            self.idle_for(record, minutes=20)
        self.store.queue_message(1, 'The user confirmed Submit.', 'm1')
        events = tick(self.launcher)
        self.assertIn({'event': 'message_delivered', 'job_id': 1, 'outcome': 'messaged'}, events)
        self.assertEqual([e['job_id'] for e in events if e['event'] == 'idle_session_parked'], [2])

    def test_a_skipped_job_brings_its_parked_applier_back_to_close_its_tabs(self):
        record, tools, request, now = self.park_on_a_question()
        self.store.ignore(1, source_id='interaction-1', sender_id='400')
        skipped = []
        events = tick(self.launcher, skip=skipped.append)
        self.assertIn({'event': 'parked_session_resumed', 'job_id': 1}, events)
        self.assertNotIn('finished_session_stopped', [e['event'] for e in events], 'the Applier may still be closing its tabs')
        self.assertEqual(tools.ask_user('Start date?', suggestions=['June 1', 'July 1'])['next'], SKIPPED_INSTRUCTION)
        self.agent().update(state='blocked')
        self.assertIn({'event': 'finished_session_stopped', 'job_id': 1}, tick(self.launcher, skip=skipped.append))

    def test_a_parked_applier_of_a_finished_job_is_stopped_for_good(self):
        record, tools = self.launch()
        tools.record_progress('ready for review')
        self.agent().update(state='done')
        now = datetime.now(timezone.utc)
        tick(self.launcher, now=now)
        tick(self.launcher, now=now + timedelta(minutes=16))
        self.assertEqual(self.state(record), 'parked')
        self.store.ignore(1, source_id='interaction-1', sender_id='400')
        self.assertIn({'event': 'finished_session_stopped', 'job_id': 1}, tick(self.launcher, skip=lambda job_id: None))
        self.assertEqual((self.attaches(), self.state(record)), ([], 'stopped'))

    def test_a_parked_applier_that_cannot_restart_stays_parked_and_alerts_once(self):
        record, tools, request, now = self.park_on_a_question()
        self.answer(request)

        def permission_prompt(background_id, message, cwd, model=None):
            raise RuntimeError('the Applier is showing a permission prompt; it needs the user, not a typed message')
        self.launcher.attach = permission_prompt
        for _ in range(2):
            self.assertEqual([e['event'] for e in tick(self.launcher)], ['parked_session_not_resumed'])
            self.assertEqual(self.state(record), 'parked')
        self.assertEqual([a['kind'] for a in self.store.alerts()], ['resume_failed'])
        self.launcher.attach = self.fake.attach
        self.assertEqual(tick(self.launcher), [{'event': 'parked_session_resumed', 'job_id': 1}])


if __name__ == '__main__':
    unittest.main()
