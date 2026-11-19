from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pipeline.applier_supervisor import tick as supervise
from pipeline.cancellation import CancellationStore
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.claude_dispatch import ClaudeServices, active_claude_owners, is_claude_owner, jobfeed_closed, jobfeed_skip, jobfeed_sync, start_claude_application, sync_claude_application, waiting_claude_owners
from pipeline.lifecycle import ApplicationState
from pipeline.progress_monitor import LocalSources, ProgressMonitor
from pipeline.worker import OrchestratorWorker, WorkerSettings
from test_claude_applier import FakeClaude
from pipeline.store import QueueItem


class FakeQueue:
    def __init__(self):
        self.items = {}

    def get(self, job_id):
        return self.items.get(job_id)

    def list_active(self):
        return list(self.items.values())

    def transition(self, job_id, state, **fields):
        current = self.items.get(job_id) or QueueItem(job_id=job_id, state=None, codex_application_session_id=None)
        self.items[job_id] = QueueItem(job_id=job_id, state=state, codex_application_session_id=fields.get('codex_application_session_id', current.codex_application_session_id))


class ClaudeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        patcher = patch('pipeline.claude_dispatch.launcher_for', return_value=self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.root / 'applications' / 'job-7'
        self.folder.mkdir(parents=True)
        self.pipeline = SimpleNamespace(
            config=SimpleNamespace(resume_root=self.root, jobfeed_db=self.root / 'jobfeed.sqlite3'),
            manifest_path=self.root / '.pipeline' / 'batches' / 'b.json',
            applications=SimpleNamespace(execution_scope='unused', execution_gate=CancellationStore(self.root / '.pipeline' / 'execution-gates.sqlite3')),
            queue=FakeQueue(), candidate_snapshots={7: {'application_url': 'https://acme.example.test/apply/7'}},
            jobfeed=SimpleNamespace(get_job=lambda job_id: SimpleNamespace(id=job_id, url='https://board.example.test/7')),
            _folder=lambda item: self.folder)
        self.job = SimpleNamespace(id=7, url='https://board.example.test/7')

    def start(self):
        return start_claude_application(self.pipeline, SimpleNamespace(job_id=7), self.job, self.folder)

    def worker(self, monitor=None):
        events = []
        self.pipeline.next_deferred_application = lambda: None
        self.pipeline.sync_claude_application = lambda job_id: sync_claude_application(self.pipeline, job_id)
        worker = OrchestratorWorker(self.pipeline, WorkerSettings(), emit=events.append, progress_monitor=monitor)
        self.addCleanup(worker.close)
        if monitor is not None:
            worker._progress_snapshot = monitor.tick(force=True)
        return worker, events

    def monitor(self, owner):
        """The batch's progress monitor, reading a queue row that says the job is Applying under the owner."""
        manifest, db = self.pipeline.manifest_path, self.pipeline.config.jobfeed_db
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(dict(application_runtime='claude', target_successful_submissions=1, max_parallel=1,
                                            ordered_candidates=[dict(id=7, company='Acme', title='Engineer')])))
        with sqlite3.connect(db) as conn:
            conn.executescript('CREATE TABLE application_queue(job_id INTEGER, state TEXT, application_folder_relpath TEXT, codex_application_session_id TEXT);'
                               'CREATE TABLE jobs(id INTEGER, url TEXT, company TEXT); CREATE TABLE applied(job_id INTEGER);')
            conn.execute("INSERT INTO jobs(id,url) VALUES(7,'https://board.example.test/7')")
            conn.execute("INSERT INTO application_queue VALUES(7,'Applying',NULL,?)", (owner,))
        return ProgressMonitor(LocalSources(self.root, manifest, db), self.root / 'monitor-output' / 'b')

    def test_a_failing_claude_inventory_skips_the_cycle_and_keeps_the_worker_alive(self):
        self.start()
        worker, events = self.worker()
        run = self.launcher.run
        for failure in (subprocess.CompletedProcess(['claude'], 1, '', 'API error'), subprocess.TimeoutExpired(['claude', 'agents'], 60)):
            def inventory(argv, failure=failure, **kwargs):
                if argv[1:2] != ['agents']:
                    return run(argv, **kwargs)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            self.launcher.run = inventory
            worker._schedule_applications()
        self.assertEqual([e['event'] for e in events], ['application_capacity_unavailable'] * 2)
        self.assertEqual(worker._application_futures, {})
        self.assertEqual(sum('--bg' in c for c in self.fake.calls), 1)

    def test_a_vanished_running_applier_alerts_the_user_once_keeps_its_slot_and_is_never_relaunched(self):
        owner = self.start()['launch_id']
        store = self.launcher.store
        store.queue_message(7, 'The user confirmed Submit in Discord for job 7.', 'dashboard-submit-x')
        self.fake.agents.clear()
        for _ in range(2):
            self.assertEqual(active_claude_owners(self.pipeline, {owner}), {owner})
            supervise(self.launcher)
            sync_claude_application(self.pipeline, 7)
        alerts = store.alerts()
        self.assertEqual([(a['job_id'], a['kind']) for a in alerts], [(7, 'session_missing')])
        self.assertIn('Job 7', alerts[0]['detail'])
        self.assertIn('needs you', alerts[0]['detail'])
        self.assertEqual(sum('--bg' in c for c in self.fake.calls), 1)
        self.assertFalse(any(c[0] == 'attach' for c in self.fake.calls))
        self.assertEqual([m['id'] for m in store.pending_messages()], ['dashboard-submit-x'])

    def test_the_worker_relaunches_a_job_claimed_before_its_launch_was_recorded(self):
        real_bind = self.launcher.store.bind

        def killed_after_bind(*args, **kwargs):
            real_bind(*args, **kwargs)
            raise SystemExit('the worker died before the launch row was written')
        with patch.object(self.launcher.store, 'bind', side_effect=killed_after_bind), self.assertRaises(SystemExit):
            self.start()
        owner = self.pipeline.queue.get(7).codex_application_session_id
        self.assertIsNone(self.launcher.launch_for(7))
        self.assertEqual(sum('--bg' in c for c in self.fake.calls), 0)
        worker, events = self.worker(self.monitor(owner))
        worker._schedule_applications()
        self.assertIn(7, worker._application_futures, events)
        self.assertEqual(worker._application_futures[7][1].result(timeout=10)['status'], 'claude_applier_relaunched')
        self.assertEqual(self.launcher.launch_for(7), owner)
        self.assertEqual(self.launcher.reconcile(owner)['state'], 'running')
        self.assertEqual(sum('--bg' in c for c in self.fake.calls), 1)

    def test_start_sets_owner_and_binding_before_launching_on_the_application_url(self):
        result = self.start()
        owner = result['launch_id']
        self.assertTrue(is_claude_owner(owner))
        self.assertEqual(result['status'], 'claude_applier_started')
        item = self.pipeline.queue.get(7)
        self.assertEqual((item.state, item.codex_application_session_id), (ApplicationState.APPLYING, owner))
        with sqlite3.connect(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            self.assertEqual(db.execute('SELECT owner,job_id FROM execution_bindings').fetchall(), [(owner, 7)])
        argv = self.fake.calls[0]
        self.assertIn('https://acme.example.test/apply/7', argv[-1])
        self.assertIn(str(self.folder.resolve()), argv)

    def test_a_second_start_reuses_the_running_applier(self):
        first = self.start()
        again = self.start()
        self.assertEqual(again['status'], 'claude_applier_running')
        self.assertEqual(again['launch_id'], first['launch_id'])
        self.assertEqual(sum('--bg' in c for c in self.fake.calls), 1)

    def test_a_job_with_another_owner_is_refused(self):
        self.pipeline.queue.transition(7, ApplicationState.APPLYING, codex_application_session_id='codex-thread-1')
        with self.assertRaisesRegex(ValueError, 'already has an application owner'):
            self.start()

    def test_sync_relaunches_under_the_same_owner_when_the_launch_never_happened(self):
        owner = 'a' * 32
        self.pipeline.queue.transition(7, ApplicationState.APPLYING, codex_application_session_id=owner)
        result = sync_claude_application(self.pipeline, 7)
        self.assertEqual(result['status'], 'claude_applier_relaunched')
        self.assertEqual(self.launcher.launch_for(7), owner)
        self.assertEqual(sync_claude_application(self.pipeline, 7)['status'], 'claude_applier_observed')

    def test_capacity_counts_running_claude_owners_only(self):
        running = self.start()['launch_id']
        self.assertEqual(active_claude_owners(self.pipeline, {running, 'b' * 32}), {running})
        self.fake.agents[0].update(state='done', pid=None)
        self.assertEqual(active_claude_owners(self.pipeline, {running}), set())

    def test_an_applier_idle_on_an_open_question_holds_no_slot_even_when_listed_as_working(self):
        owner = self.start()['launch_id']
        self.launcher.store.ask(7, owner, 'question', 'Which F-1 status applies?')
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), set())
        self.fake.agents[0].update(status='idle')
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), {owner})
        self.fake.agents[0].update(status='busy')
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), set())

    def test_an_applier_long_idle_with_nothing_open_holds_no_slot(self):
        owner = self.start()['launch_id']
        self.fake.agents[0].update(status='idle')
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), set())
        long_ago = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        self.launcher._update(owner, idle_since=long_ago)
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), {owner})
        self.fake.agents[0].update(status='busy')
        self.assertEqual(waiting_claude_owners(self.pipeline, {owner}), set())

    def test_a_refused_launch_holds_no_slot_so_the_sync_can_start_it_again(self):
        self.fake.refusal = 'Workspace not trusted. Run `claude` in /x once and accept the trust prompt, then retry.\n'
        owner = self.start()['launch_id']
        # Launches recorded before launch_failed existed say unknown, with no session.
        for state in ('launch_failed', 'unknown'):
            self.launcher._update(owner, state=state)
            self.assertEqual(active_claude_owners(self.pipeline, {owner}), set(), state)
        self.fake.refusal = None
        self.assertEqual(sync_claude_application(self.pipeline, 7)['state'], 'running')
        self.assertEqual(active_claude_owners(self.pipeline, {owner}), {owner})

    def test_the_worker_counts_a_running_claude_owner_as_active(self):
        owner = self.start()['launch_id']
        pipeline = Mock()
        pipeline.config.resume_root = self.root
        pipeline.queue.list_active.return_value = [QueueItem(job_id=7, state=ApplicationState.APPLYING, codex_application_session_id=owner)]
        worker = OrchestratorWorker(pipeline, WorkerSettings(), emit=lambda e: None)
        self.addCleanup(worker.close)
        self.assertEqual(worker._active_application_threads(), {owner})

    def test_services_run_at_their_own_pace(self):
        now = [0.0]
        bridge = Mock()
        bridge.sync.return_value = [{'event': 'posted'}]
        env = self.root / 'discord.env'
        services = ClaudeServices(self.pipeline, clock=lambda: now[0], bridge_factory=lambda: bridge, env_path=env)
        with patch('pipeline.applier_supervisor.tick', return_value=[{'event': 'alert'}]) as supervise:
            self.assertEqual(services.tick(), [{'event': 'alert'}])
            env.write_text('DISCORD_BOT_TOKEN=x\n')
            now[0] = 6.0
            self.assertEqual(services.tick(), [{'event': 'posted'}])
            now[0] = 61.0
            services.tick()
            self.assertEqual(supervise.call_count, 2)
            self.assertIsNotNone(supervise.call_args.kwargs['sync'])
            self.assertIsNotNone(supervise.call_args.kwargs['skip'])

    def test_a_mail_event_reported_at_any_point_of_a_tick_is_returned_exactly_once(self):
        event = {'event': 'confirmation_mail_answered', 'job_id': 7}

        monitoring, tool, tick = sys.monitoring, sys.monitoring.DEBUGGER_ID, ClaudeServices.tick.__code__

        def run(inject_at):
            services = ClaudeServices(self.pipeline, env_path=self.root / 'none.env')
            seen = [0]

            def instruction(code, offset):
                seen[0] += 1
                if seen[0] == inject_at:
                    services._check_mail()

            monitoring.use_tool_id(tool, 'mail-race')
            monitoring.register_callback(tool, monitoring.events.INSTRUCTION, instruction)
            monitoring.set_local_events(tool, tick, monitoring.events.INSTRUCTION)
            try:
                first = services.tick()
            finally:
                monitoring.set_local_events(tool, tick, 0)
                monitoring.free_tool_id(tool)
            return first + services.tick(), seen[0]

        with patch('pipeline.applier_supervisor.tick', return_value=[]), patch('pipeline.captcha_holds.park', return_value=[]), \
                patch('pipeline.claude_dispatch.launcher_for'), patch('pipeline.confirmation_mail.check', return_value=[event]):
            _, boundaries = run(0)
            self.assertGreater(boundaries, 0)
            for point in range(1, boundaries + 1):
                with self.subTest(point=point):
                    self.assertEqual(run(point)[0], [event])

    def test_drain_happens_once_when_the_worker_is_stopped(self):
        self.start()
        services = ClaudeServices(self.pipeline, env_path=self.root / 'none.env')
        self.assertEqual(services.drain(), {'stopped': [], 'working': [7], 'ended': []})
        self.assertIsNone(services.drain())
        self.assertEqual(self.launcher.store.control('draining') is not None, True)

    def test_the_sync_callback_raises_unless_jobfeed_confirms(self):
        self.pipeline.finish_submission = Mock(return_value={'status': 'submission_unknown', 'reason': 'confirmation_artifact_required'})
        with self.assertRaisesRegex(RuntimeError, 'confirmation_artifact_required'):
            jobfeed_sync(self.pipeline)(7)
        self.pipeline.finish_submission = Mock(return_value={'status': 'submission_completed'})
        self.assertEqual(jobfeed_sync(self.pipeline)(7)['status'], 'submission_completed')

    def test_a_user_skip_cancels_the_job_and_marks_it_ignored_once(self):
        owner = self.start()['launch_id']
        store = self.launcher.store
        request = store.ask(7, owner, 'question', 'Cloudflare check. What now?')
        store.reply(request, 'SKIP THIS POSTING AND MARK IT AS IGNORED', source_kind='discord', source_id='m1', sender_id='400')
        store.record_skip(7, owner, request, 1)
        ignored = []
        self.pipeline.cancel = lambda job_id: self.pipeline.queue.transition(job_id, ApplicationState.CANCELLED)
        self.pipeline.jobfeed.is_ignored = lambda job_id: job_id in [j for j, _, _ in ignored]
        self.pipeline.jobfeed.record_ignored = lambda job_id, note, prefix: ignored.append((job_id, note, prefix))
        skip = jobfeed_skip(self.pipeline)
        skip(7)
        skip(7)
        self.assertEqual(self.pipeline.queue.get(7).state, ApplicationState.CANCELLED)
        self.assertEqual(ignored, [(7, 'SKIP THIS POSTING AND MARK IT AS IGNORED', 'User asked to skip this posting: ')])
        stops = lambda: [c[1] for c in self.fake.calls if c[0] == 'claude' and c[1] == 'stop']
        self.assertEqual(stops(), [], 'the Applier first closes its tabs')
        self.fake.agents[0]['state'] = 'blocked'
        supervise(self.launcher, skip=skip)
        self.assertEqual(stops(), ['stop'])

    def test_a_closed_posting_stops_the_applier_and_marks_the_job_unavailable_without_asking(self):
        owner = self.start()['launch_id']
        self.launcher.store.record_closed(7, owner, 'This job has expired on Indeed')
        ignored = []
        self.pipeline.cancel = Mock(side_effect=AssertionError('a closed posting is unavailable, not cancelled'))
        self.pipeline.jobfeed.is_ignored = lambda job_id: job_id in [j for j, _, _ in ignored]
        self.pipeline.jobfeed.record_ignored = lambda job_id, note, prefix: ignored.append((job_id, note, prefix))
        closed = jobfeed_closed(self.pipeline)
        closed(7)
        closed(7)
        self.assertEqual(self.pipeline.queue.get(7).state, ApplicationState.UNAVAILABLE)
        self.assertEqual(ignored, [(7, 'This job has expired on Indeed', 'Employer posting closed: ')])


if __name__ == '__main__':
    unittest.main()
