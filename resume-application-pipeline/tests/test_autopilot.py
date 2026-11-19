"""Autopilot mode and its continuous Claude batch; synthetic fixtures, no real worker, Applier or Jobfeed service."""
from concurrent.futures import Future, wait
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import recruiter_sheet_fixture as sheet
from test_claude_applier import FakeClaude
from runtime_fixture import RuntimeFixture
from pipeline import autopilot
from pipeline.applier_mcp import ApplierTools
from pipeline.applier_runtime import ApplierRuntime
from pipeline.batch import BatchPipeline
from pipeline.cancellation import CancellationStore
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.local_state import private_json
from pipeline.claude_dashboard_start import Host
from pipeline.claude_dispatch import ClaudeServices, start_claude_application
from pipeline.config import PipelineConfig
from pipeline.jobfeed import Job
from pipeline.lifecycle import ApplicationState as S
from pipeline.progress_monitor import digest
from pipeline.requeue import requeue
from pipeline.resume_catalog import prepare_application, publish, verify_selection
from pipeline.store import Hold
from pipeline.worker import OrchestratorWorker, WorkerSettings


def job(i, company='Fixture Company'):
    return Job(i, company, f'Engineer {i}', f'Complete JD {i}', f'https://fixture.invalid/jobs/{i}')


def observed(job_id, **fields):
    return {'job_id': job_id, 'owner': None, 'terminal': False, 'submitted': False, 'synced': False} | fields


class AutopilotBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = RuntimeFixture(temp.name)
        sheet.install(self.f)
        self.root = self.f.root
        self.runtime = ApplierRuntime(self.root)
        self.eligible = [job(i) for i in range(1, 7)]
        self.queries = []
        self.active = set()
        patcher = patch('pipeline.claude_dispatch.active_claude_owners', lambda pipeline, owners: self.active & owners)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pipeline = self.load()

    def load(self):
        pipeline = BatchPipeline(PipelineConfig.discover(self.root), autopilot.write_manifest(self.root))
        pipeline.initialize()

        def priority_jobs(**query):
            self.queries.append(query)
            skip = query.get('skip') or (lambda job: False)
            return [job for job in self.eligible if not skip(job)][:query.get('limit', 100)]
        pipeline.jobfeed.priority_jobs = priority_jobs
        return pipeline

    def turn(self, on):
        self.runtime.set_autopilot(on, actor='user via test', source='test')

    def refill(self):
        self.pipeline.autopilot_next_query = 0
        added = self.pipeline.refill_queue(100)
        self.pipeline.reload_manifest()
        return added

    def members(self):
        return [job['id'] for job in json.loads(autopilot.manifest_path(self.root).read_text())['ordered_candidates']]

    def test_off_admits_nothing(self):
        self.assertEqual(self.refill(), [])
        self.assertEqual((self.members(), self.queries), ([], []))

    def test_on_admits_apply_and_consider_jobs_up_to_the_free_slots(self):
        self.turn(True)
        self.assertEqual(self.refill(), [1, 2, 3, 4])
        self.assertEqual(self.queries[0]['verdicts'], {'apply', 'consider'})
        self.assertEqual(self.members(), [1, 2, 3, 4])
        manifest = json.loads(autopilot.manifest_path(self.root).read_text())
        self.assertEqual((manifest['target_successful_submissions'], manifest['resume_mode'], manifest['max_parallel']),
                         (4, 'selected_existing', 4))
        self.assertEqual({self.pipeline.queue.get(i).state for i in range(1, 5)}, {S.QUEUED})
        self.assertIsNone(self.pipeline.queue.get(5))

    def test_admitted_jobs_that_have_not_started_keep_their_slots(self):
        self.turn(True)
        self.refill()
        self.assertEqual(self.refill(), [])
        self.assertEqual(len(self.queries), 1)

    def test_busy_appliers_leave_only_their_free_slots(self):
        self.turn(True)
        self.refill()
        for i in (1, 2, 3):
            self.pipeline.queue.transition(i, S.RESUME_READY)
            self.pipeline.queue.transition(i, S.APPLYING, codex_application_session_id=f'claude:{i}')
        self.pipeline.queue.transition(4, S.CANCELLED)
        self.active = {'claude:1', 'claude:2', 'claude:3'}
        self.assertEqual(self.refill(), [5])

    def test_a_job_that_cannot_start_holds_only_its_own_slot(self):
        self.turn(True)
        self.refill()
        for i in (1, 2):
            self.pipeline.queue.transition(i, S.CANCELLED)
        self.assertEqual(self.refill(), [5, 6])

    def test_excluded_brands_and_held_jobs_are_not_admitted(self):
        self.turn(True)
        self.eligible = [job(1, 'TikTok'), job(2), job(3)]
        self.pipeline.queue.admit(2)
        self.pipeline.queue.hold(2, Hold(id='captcha-2', kind='captcha', detail='CAPTCHA needed', page_url=None, created_at='2026-11-06T00:00:00+00:00'))
        self.assertEqual(self.refill(), [3])

    def scans(self, *, unit='jobfeed-autopilot-cycle.service', last=None):
        """Refill with no eligible job left, and return the units the worker asked systemd to start."""
        with closing(sqlite3.connect(self.pipeline.config.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS pipeline_runs(started_at TEXT, source TEXT)')
            db.execute('DELETE FROM pipeline_runs')
            if last is not None:
                db.execute("INSERT INTO pipeline_runs VALUES(?, 'all')", (last.isoformat().replace('+00:00', 'Z'),))
        started = []
        with patch.dict('os.environ', {autopilot.SCAN_UNIT_ENV: unit} if unit else {}), \
                patch.object(autopilot, 'start_unit', lambda name: started.append(name)):
            self.refill()
        return started

    def test_a_dry_queue_starts_one_scan_and_then_waits(self):
        self.turn(True)
        self.eligible = []
        self.assertEqual(self.scans(), ['jobfeed-autopilot-cycle.service'])
        self.assertEqual(self.scans(), [])
        self.pipeline.autopilot_next_scan = 0
        now = datetime.now(timezone.utc)
        self.assertEqual(self.scans(last=now - timedelta(hours=1)), [])
        self.pipeline.autopilot_next_scan = 0
        self.assertEqual(self.scans(last=now - timedelta(hours=4)), ['jobfeed-autopilot-cycle.service'])

    def test_no_scan_while_jobs_are_admitted_off_or_outside_the_autopilot_service(self):
        self.turn(True)
        self.assertEqual(self.scans(), [])
        self.eligible = []
        self.assertEqual(self.scans(unit=None), [])
        self.turn(False)
        self.assertEqual(self.scans(), [])

    def test_starts_only_while_autopilot_is_on(self):
        self.turn(True)
        self.refill()
        self.assertTrue(self.pipeline.can_start_application_for(1))
        self.turn(False)
        self.assertFalse(self.pipeline.can_start_application_for(1))

    def test_never_finishes_while_on(self):
        self.turn(True)
        self.assertFalse(self.pipeline.finished({'complete': True, 'jobs': []}))
        self.assertFalse(self.pipeline.finished({'complete': True, 'jobs': [observed(1, owner='claude:1', submitted=True, synced=True)]}))

    def test_off_finishes_once_no_started_job_needs_the_worker(self):
        self.turn(False)
        settled = [observed(1, owner='claude:1', submitted=True, synced=True), observed(2, owner='claude:2', terminal=True), observed(3)]
        self.assertTrue(self.pipeline.finished({'complete': False, 'jobs': settled}))
        self.assertFalse(self.pipeline.finished({'complete': False, 'jobs': [*settled, observed(4, owner='claude:4')]}))
        self.assertFalse(self.pipeline.finished({'complete': False, 'jobs': [*settled, observed(5, owner='claude:5', submitted=True)]}))

    def test_worker_keeps_running_after_completion_only_while_on(self):
        for on, stops in ((True, False), (False, True)):
            self.turn(on)
            monitor = Mock(ledger=None)
            monitor.tick.return_value = {'config_hash': self.pipeline.loaded_manifest_hash, 'complete': True, 'target': 0,
                                         'max_per_company': None, 'counts': {'submitted': 0, 'reserved': 0},
                                         'errors': [], 'dispatch_errors': [], 'jobs': []}
            worker = OrchestratorWorker(self.pipeline, WorkerSettings(), emit=lambda event: None, progress_monitor=monitor)
            self.addCleanup(worker.close)
            worker._check_progress()
            self.assertEqual(worker._progress_done, stops)

    def test_selected_batch_still_admits_nothing_and_finishes_on_completion(self):
        self.turn(True)
        manifest = json.loads(autopilot.manifest_path(self.root).read_text())
        manifest.pop('continuous')
        path = self.root / '.pipeline/batches/selected.json'
        path.write_text(json.dumps(manifest))
        selected = BatchPipeline(PipelineConfig.discover(self.root), path)
        self.assertEqual(selected.refill_queue(100), [])
        self.assertTrue(selected.finished({'complete': True, 'jobs': []}))

    def test_write_manifest_keeps_members_and_refreezes_the_catalog(self):
        self.turn(True)
        self.refill()
        autopilot.write_manifest(self.root)
        self.assertEqual(self.members(), [1, 2, 3, 4])


class RealJobfeedRefillTests(unittest.TestCase):
    """The real BatchPipeline wrapper over the real Jobfeed; only the dashboard's HTTP answer is synthetic."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = RuntimeFixture(temp.name)
        sheet.install(self.f)
        ApplierRuntime(self.f.root).set_autopilot(True, actor='user via test', source='test')
        self.pipeline = BatchPipeline(PipelineConfig.discover(self.f.root), autopilot.write_manifest(self.f.root))
        self.pipeline.initialize()
        rows = [{'real_job_id': str(1000 + i), 'source_job_id': i, 'verdict': verdict, 'decision': 'results'}
                for i, verdict in ((1, 'apply'), (2, 'consider'), (3, 'skip'))]
        self.pipeline.jobfeed.transport = lambda url: {'jobs': rows, 'total': len(rows)}

    def test_refill_admits_through_the_batch_wrapper_and_real_priority_jobs(self):
        self.assertEqual(self.pipeline.refill_queue(100), [1, 2])
        self.assertEqual([job['title'] for job in json.loads(autopilot.manifest_path(self.f.root).read_text())['ordered_candidates']],
                         ['Engineer 1', 'Engineer 2'])

    def test_the_batch_wrapper_still_refuses_an_incomplete_source_when_asked_for_an_open_job(self):
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET title='' WHERE id=1")
        self.assertIsNone(self.pipeline.jobfeed.dispatchable_job(1))
        self.assertEqual(self.pipeline.jobfeed.get_job(1).company, 'Fixture Company')
        self.assertEqual(self.pipeline.jobfeed.dispatchable_job(2).title, 'Engineer 2')


class StrandedFixture(unittest.TestCase):
    """Jobs a dead worker of an older batch left behind; real stores and requeue, fake Claude CLI."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = RuntimeFixture(temp.name)
        sheet.install(self.f)
        self.root = self.f.root
        self.config = PipelineConfig.discover(self.root)
        self.store = ApplierRuntime(self.root)
        self.store.set_autopilot(True, actor='user via test', source='test')
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        self.service_down = None
        for target, fake in (('pipeline.claude_dispatch.launcher_for', lambda *_: self.launcher), ('pipeline.requeue.stopped_service', lambda *_: self.service_down)):
            patcher = patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.old = self.root / '.pipeline/batches/dashboard-claude-old.json'
        self.old.parent.mkdir(parents=True, exist_ok=True)
        self.old.write_text(json.dumps({'application_runtime': 'claude', 'ordered_candidates': [{'id': i} for i in range(1, 21)]}))
        self.pipeline = BatchPipeline(self.config, autopilot.write_manifest(self.root))
        self.pipeline.initialize()
        self.queue = self.pipeline.queue
        self.eligible = []
        self.queries = 0
        self.pipeline.jobfeed.transport = self.results
        self.events = []
        self.pipeline.emit = self.events.append

    def results(self, url):
        self.queries += 1
        rows = [{'real_job_id': str(1000 + i), 'source_job_id': i, 'verdict': 'apply', 'decision': 'results'} for i in self.eligible]
        return {'jobs': rows, 'total': len(rows)}

    def resume_ready(self, job_id):
        self.queue.admit(job_id)
        self.queue.transition(job_id, S.RESUME_READY, application_folder_relpath=f'existing-old-{job_id}')
        folder = self.config.applications_root / f'existing-old-{job_id}'
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def stranded(self, job_id, *, session_state=None):
        """An Applier from the older batch. Its session is gone unless session_state says what `claude agents` still lists."""
        folder = self.resume_ready(job_id)
        gate = CancellationStore(self.root / '.pipeline/execution-gates.sqlite3')
        pipeline = SimpleNamespace(config=self.config, manifest_path=self.old, candidate_snapshots={}, queue=self.queue,
                                   applications=SimpleNamespace(execution_scope='unused', execution_gate=gate))
        job = SimpleNamespace(id=job_id, url=f'https://fixture.invalid/jobs/{job_id}')
        owner = start_claude_application(pipeline, self.queue.get(job_id), job, folder)['launch_id']
        agent = self.fake.agents.pop()
        if session_state:
            self.fake.agents.append(agent | {'state': session_state})
        return owner

    def manifest(self):
        return json.loads(autopilot.manifest_path(self.root).read_text())

    def members(self):
        return [job['id'] for job in self.manifest()['ordered_candidates']]


class StrandedJobTests(StrandedFixture):
    def tick(self):
        self.pipeline.autopilot_next_adoption = self.pipeline.autopilot_next_query = 0
        return self.pipeline.refill_queue(100)

    def inventory_reads(self):
        return sum(call[1:3] == ['agents', '--json'] for call in self.fake.calls)

    def refusals(self, job_id):
        return [event for event in self.events if event['event'] == 'stranded_job_refused' and event['job_id'] == job_id]

    def test_a_stranded_applier_is_retired_and_its_job_joins_the_batch_with_the_full_snapshot(self):
        owner = self.stranded(1)
        self.assertEqual(self.tick(), [1])
        self.assertEqual(self.manifest()['ordered_candidates'], [{'id': 1, 'real_job_id': '1001', 'company': 'Fixture Company', 'title': 'Engineer 1',
                                                                  'url': 'https://fixture.invalid/jobs/1', 'jd_text': 'Complete JD 1'}])
        self.assertEqual(self.manifest()['target_successful_submissions'], 1)
        item = self.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id), (S.QUEUED, None))
        [record] = self.store.requeues(1)
        self.assertEqual((record['actor'], record['retired_owner'], record['batches']), ('autopilot', owner, [str(self.old)]))
        self.assertEqual(json.loads(self.old.read_text())['suspended_job_ids'], [1])
        self.assertEqual([event['job_id'] for event in self.events if event['event'] == 'stranded_job_adopted'], [1])

    def test_an_adopted_job_is_prepared_by_this_batch_after_the_reload(self):
        self.stranded(1)
        self.tick()
        self.pipeline.reload_manifest()
        self.assertEqual(self.pipeline.process_completions(), [1])
        self.assertEqual(self.queue.get(1).state, S.RESUME_READY)

    def test_a_stranded_resume_ready_job_joins_the_batch(self):
        self.resume_ready(2)
        self.assertEqual(self.tick(), [2])
        self.assertEqual(self.queue.get(2).state, S.QUEUED)
        self.assertEqual(self.store.requeues(2)[0]['actor'], 'autopilot')

    def test_a_job_with_a_request_still_live_is_left_alone(self):
        owners = {job_id: self.stranded(job_id) for job_id in (1, 2, 3)}
        self.store.ask(1, owners[1], 'question', 'Start date?', field='start')
        answered = self.store.ask(2, owners[2], 'question', 'Which file?', field='file')
        self.store.reply(answered, 'This one', source_kind='discord', source_id='m1', sender_id='400')
        self.assertEqual(self.tick(), [3])
        for job_id in (1, 2):
            self.assertEqual((self.queue.get(job_id).state, self.queue.get(job_id).codex_application_session_id), (S.APPLYING, owners[job_id]))
            self.assertEqual(self.store.requeues(job_id), [])
            self.assertEqual(self.refusals(job_id), [])

    def test_submission_evidence_refuses_the_job_and_reports_it_once(self):
        owner = self.stranded(1)
        (self.config.applications_root / 'existing-old-1/submission-receipt.json').write_text('{}')
        for _ in range(3):
            self.assertEqual(self.tick(), [])
        self.assertEqual((self.queue.get(1).state, self.queue.get(1).codex_application_session_id), (S.APPLYING, owner))
        self.assertEqual(self.store.requeues(1), [])
        self.assertEqual(self.members(), [])
        [refusal] = self.refusals(1)
        self.assertIn('submission-receipt.json', refusal['reason'])

    def test_a_session_still_running_is_refused_until_it_has_ended(self):
        self.stranded(1, session_state='working')
        self.assertEqual((self.tick(), self.tick()), ([], []))
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        self.assertEqual(len(self.refusals(1)), 1)
        self.fake.agents[0]['state'] = 'stopped'
        self.assertEqual(self.tick(), [1])

    def test_the_stopped_service_proof_lets_a_listed_session_be_adopted(self):
        self.stranded(1, session_state='working')
        self.service_down = 'the Claude background service is down'
        self.assertEqual(self.tick(), [1])

    def test_running_it_again_adds_nothing_and_keeps_one_record(self):
        self.stranded(1)
        self.assertEqual(self.tick(), [1])
        self.pipeline.reload_manifest()
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.members(), [1])
        self.assertEqual(len(self.store.requeues(1)), 1)

    def test_a_job_requeued_just_before_a_crash_is_still_added(self):
        self.stranded(1)
        with patch('pipeline.autopilot.private_json', side_effect=OSError('crashed before the manifest write')), self.assertRaises(OSError):
            self.tick()
        self.assertEqual((self.queue.get(1).state, self.members()), (S.QUEUED, []))
        self.assertEqual(self.tick(), [1])
        self.assertEqual(self.members(), [1])
        self.assertEqual(len(self.store.requeues(1)), 1)

    def test_a_job_the_user_requeued_is_not_taken_as_a_crashed_adoption(self):
        self.stranded(1)
        self.assertEqual(requeue(self.config, [1], 'user asked', worker_busy=lambda _: False, inventory=self.launcher.agents)[0]['status'], 'requeued')
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.members(), [])

    def test_off_touches_nothing_and_reads_no_inventory(self):
        self.stranded(1)
        self.store.set_autopilot(False, actor='user via test', source='test')
        self.fake.calls.clear()
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.queue.get(1).state, self.members(), self.store.requeues(1), self.inventory_reads()), (S.APPLYING, [], [], 0))

    def test_a_selected_batch_adopts_nothing(self):
        self.stranded(1)
        manifest = self.manifest()
        manifest.pop('continuous')
        path = self.root / '.pipeline/batches/selected.json'
        path.write_text(json.dumps(manifest))
        selected = BatchPipeline(self.config, path)
        self.assertEqual(selected.refill_queue(100), [])
        self.assertEqual(self.queue.get(1).state, S.APPLYING)

    def test_the_inventory_is_read_only_when_there_are_candidates(self):
        self.assertEqual((self.tick(), self.inventory_reads()), ([], 0))
        self.stranded(1)
        self.fake.calls.clear()
        self.assertEqual(self.tick(), [1])
        self.assertEqual(self.inventory_reads(), 1)

    def test_a_held_job_is_left_alone(self):
        self.stranded(1)
        self.queue.hold(1, Hold(id='captcha-1', kind='captcha', detail='CAPTCHA needed', page_url=None, created_at='2026-11-06T00:00:00+00:00'))
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.queue.get(1).state, S.APPLYING)

    def test_a_closed_or_incomplete_posting_is_not_requeued(self):
        self.stranded(1)
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET company='' WHERE id=1")
        self.assertEqual((self.tick(), self.tick()), ([], []))
        self.assertEqual((self.queue.get(1).state, self.store.requeues(1)), (S.APPLYING, []))
        self.assertEqual(len(self.refusals(1)), 1)

    def test_an_excluded_company_or_ignored_posting_is_not_requeued(self):
        self.stranded(1)
        self.stranded(2)
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET company='TikTok' WHERE id=1")
            db.execute("INSERT INTO real_job_status(real_job_id,status) VALUES('1002','ignored')")
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.queue.get(1).state, self.queue.get(2).state), (S.APPLYING, S.APPLYING))

    def test_two_sources_of_one_posting_are_not_both_added(self):
        self.stranded(1)
        self.stranded(2)
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET real_job_id='1001' WHERE id=2")
        self.assertEqual(self.tick(), [1])
        self.assertEqual((self.members(), self.queue.get(2).state), ([1], S.APPLYING))
        self.assertEqual(len(self.refusals(2)), 1)

    def test_adopted_jobs_take_free_slots_before_the_worker_reloads(self):
        self.eligible = [11, 12, 13, 14, 15, 16]
        self.stranded(1)
        self.stranded(2)
        self.assertEqual(self.tick(), [1, 2, 11, 12])
        self.assertEqual(self.tick(), [])
        self.pipeline.reload_manifest()
        self.assertEqual(self.tick(), [])

    def test_more_stranded_jobs_than_slots_are_all_adopted_and_new_admissions_wait(self):
        self.eligible = [11]
        for job_id in range(1, 7):
            self.stranded(job_id)
        self.assertEqual(self.tick(), [1, 2, 3, 4, 5, 6])
        self.assertEqual((self.queries, self.queue.get(11)), (0, None))

    def test_a_failed_read_of_new_jobs_keeps_the_adoption(self):
        self.stranded(1)
        self.pipeline.jobfeed.transport = Mock(side_effect=OSError('dashboard is down'))
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.members(), [1])


class Inline:
    """An executor that runs each call before submit returns."""

    def submit(self, call, *args):
        future = Future()
        try:
            future.set_result(call(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **_):
        pass


class WorkerFixture(StrandedFixture):
    """A real worker over the real stores, whose applications run at once."""

    def worker(self, pipeline=None):
        def snapshot():
            return {'config_hash': digest(self.manifest()), 'complete': False, 'target': 100, 'max_per_company': None,
                    'counts': {'submitted': 0, 'reserved': 0}, 'errors': [], 'dispatch_errors': [], 'jobs': []}
        monitor = Mock(ledger=None)
        monitor.tick.side_effect = snapshot
        pipeline = pipeline or self.pipeline
        worker = OrchestratorWorker(pipeline, WorkerSettings(), emit=self.events.append, progress_monitor=monitor)
        worker._claude_services = ClaudeServices(pipeline, env_path=self.root / 'no-discord.env')
        # An application started on the first tick runs at once, so it cannot lose a race with the refill thread by luck.
        worker._applications.shutdown()
        worker._applications = Inline()
        self.addCleanup(worker.close)
        return worker

    def rebooted(self):
        """`claude agents` lists job 1's session as working, and a new background launch ends the proof that it is gone."""
        self.stranded(1, session_state='working')
        self.launched_while = []
        run = self.launcher.run

        def recording_run(argv, **kwargs):
            if '--bg' in argv:
                self.launched_while.append(self.queue.get(1).state)
                self.service_down = None
            return run(argv, **kwargs)
        self.launcher.run = recording_run
        self.service_down = 'the Claude background service is down'

    def member_ready_to_start(self, job_id):
        """A job the batch admitted before the restart, which the first tick starts unless something holds it back."""
        self.queue.admit(job_id)
        manifest = self.manifest()
        manifest['ordered_candidates'].append({'id': job_id, 'real_job_id': str(1000 + job_id), 'company': 'Fixture Company', 'title': f'Engineer {job_id}',
                                               'url': f'https://fixture.invalid/jobs/{job_id}', 'jd_text': f'Complete JD {job_id}'})
        manifest['target_successful_submissions'] = len(manifest['ordered_candidates'])
        private_json(autopilot.manifest_path(self.root), manifest, self.root)
        self.pipeline.reload_manifest()

    def settle(self, worker):
        running = [future for _, future in worker._application_futures.values()] + ([worker._producer_future] if worker._producer_future else [])
        wait(running)
        worker._reap()


class StrandedWorkerOrderTests(WorkerFixture):
    """The worker's first tick. After a reboot `claude agents` still lists dead sessions, and the only proof that they
    ended is a service status file that the first new `claude --bg` rewrites."""

    def test_adoption_finishes_before_the_first_tick_can_start_an_applier(self):
        self.rebooted()
        self.member_ready_to_start(2)
        worker = self.worker()
        worker.tick()
        self.settle(worker)
        self.assertEqual(self.launched_while, [S.QUEUED], "the batch's own job started only after the stranded job was adopted")
        self.assertIn(1, self.members())

    def test_adoption_finishes_before_the_supervisor_can_wake_a_session(self):
        owner = self.stranded(3, session_state='blocked')
        self.store.ask(3, owner, 'question', 'Start date?', field='start')
        self.store.queue_message(3, 'Wake up.', 'wake-3')
        self.rebooted()
        attach, states = self.launcher.attach, []

        def recording_attach(*args, **kwargs):
            states.append(self.queue.get(1).state)
            return attach(*args, **kwargs)
        self.launcher.attach = recording_attach
        self.worker().tick()
        self.assertEqual(states, [S.QUEUED])

    def test_the_adopted_job_reaches_the_reload_and_starts_under_a_new_owner(self):
        self.rebooted()
        worker = self.worker()
        worker.tick()
        self.settle(worker)
        self.assertIn(1, self.pipeline.batch_ids, 'the first tick reloaded the manifest')
        for _ in range(2):
            worker.tick()
            self.settle(worker)
        item = self.queue.get(1)
        self.assertEqual(item.state, S.APPLYING)
        self.assertNotEqual(item.codex_application_session_id, self.store.requeues(1)[0]['retired_owner'])

    def test_a_contended_database_during_adoption_starts_nothing_and_is_retried(self):
        self.rebooted()
        self.member_ready_to_start(2)
        worker = self.worker()
        with patch('pipeline.applier_runtime.ApplierRuntime.requests', side_effect=sqlite3.OperationalError('database is locked')), self.assertRaises(sqlite3.OperationalError):
            worker.tick()
        self.assertEqual(self.launched_while, [])
        worker.tick()
        self.settle(worker)
        self.assertEqual(self.launched_while, [S.QUEUED])

    def test_a_selected_batch_worker_adopts_nothing(self):
        self.stranded(1)
        manifest = self.manifest()
        manifest.pop('continuous')
        path = self.root / '.pipeline/batches/selected.json'
        path.write_text(json.dumps(manifest))
        worker = self.worker(BatchPipeline(self.config, path))
        worker.tick()
        self.settle(worker)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)


class RecruiterSheetHoldTests(WorkerFixture):
    """Autopilot must not apply to a job on the recruiter sheet unless the user chose apply_myself."""

    def tick(self):
        self.pipeline.autopilot_next_adoption = self.pipeline.autopilot_next_query = 0
        self.pipeline.autopilot_sheet_read = (None, None)
        return self.pipeline.refill_queue(100)

    def run_tick(self, worker):
        self.pipeline.autopilot_sheet_read = (None, None)
        worker.tick()
        self.settle(worker)

    def started(self, job_id):
        return self.queue.get(job_id).state != S.QUEUED

    def break_sheet(self):
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute('ALTER TABLE recruiter_sheet_rows RENAME TO recruiter_sheet_rows_broken')

    def repair_sheet(self):
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute('ALTER TABLE recruiter_sheet_rows_broken RENAME TO recruiter_sheet_rows')

    def unavailable_events(self):
        return [event for event in self.events if event.get('event') == 'recruiter_sheet_unavailable']

    def test_a_job_on_the_sheet_is_not_admitted_and_creates_no_queue_row(self):
        sheet.qualify(self.f, 1)
        self.eligible = [1, 2]
        self.assertEqual(self.tick(), [2])
        self.assertEqual(self.members(), [2])
        self.assertIsNone(self.queue.get(1))

    def test_apply_myself_lets_refill_admit_it(self):
        sheet.qualify(self.f, 1)
        self.eligible = [1]
        self.assertEqual(self.tick(), [])
        sheet.decide(self.f, 1, 'apply_myself')
        self.assertEqual(self.tick(), [1])

    def test_many_jobs_on_the_sheet_do_not_use_up_the_jobs_refill_reads(self):
        for job_id in range(1, 26):
            sheet.qualify(self.f, job_id)
        self.eligible = list(range(1, 31))
        self.assertEqual(self.tick(), [26, 27, 28, 29])

    def test_many_captcha_holds_do_not_use_up_the_jobs_refill_reads(self):
        for job_id in range(1, 41):
            self.queue.admit(job_id)
            self.queue.hold(job_id, Hold(id=f'captcha-{job_id}', kind='captcha', detail='CAPTCHA needed', page_url=None,
                                         created_at='2026-11-09T00:00:00+00:00'))
        self.eligible = list(range(1, 46))
        self.assertEqual(self.tick(), [41, 42, 43, 44])

    def test_a_queued_batch_member_on_the_sheet_does_not_start_while_the_others_do(self):
        self.member_ready_to_start(2)
        self.member_ready_to_start(3)
        sheet.qualify(self.f, 2)
        worker = self.worker()
        self.run_tick(worker)
        self.assertEqual(self.queue.get(2).state, S.QUEUED)
        self.assertIsNone(self.queue.get(2).application_folder_relpath)
        self.assertTrue(self.started(3))

    def test_apply_myself_releases_a_held_member_on_the_next_tick(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        worker = self.worker()
        self.run_tick(worker)
        self.assertFalse(self.started(2))
        sheet.decide(self.f, 2, 'apply_myself')
        self.run_tick(worker)
        self.assertTrue(self.started(2))

    def test_every_other_status_keeps_a_member_held(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        worker = self.worker()
        for status in ('asked', 'with_recruiter', 'passed'):
            sheet.decide(self.f, 2, status)
            self.run_tick(worker)
            self.assertFalse(self.started(2), status)

    def test_a_held_member_takes_no_slot_so_refill_fills_it(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        self.eligible = [3, 4, 5, 6, 7]
        self.assertEqual(self.tick(), [3, 4, 5, 6])

    def test_a_job_that_stops_qualifying_with_no_decision_is_released(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET location='Buffalo, NY' WHERE id=2")
        self.assertEqual(sheet.rows(self.f), [])
        worker = self.worker()
        self.run_tick(worker)
        self.assertTrue(self.started(2))

    def test_a_job_with_no_funding_check_yet_is_held(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        [row] = sheet.rows(self.f)
        self.assertTrue(row.funding_pending)
        worker = self.worker()
        self.run_tick(worker)
        self.assertFalse(self.started(2))

    def test_a_job_that_pays_too_little_is_not_held(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute("UPDATE jobs SET jd_text='Pay: $90,000 - $110,000 per year.' WHERE id=2")
        self.assertEqual(sheet.rows(self.f), [])
        worker = self.worker()
        self.run_tick(worker)
        self.assertTrue(self.started(2))

    def test_a_prepared_job_with_a_decision_does_not_start(self):
        sheet.qualify(self.f, 2)
        sheet.decide(self.f, 2, 'asked')
        self.member_ready_to_start(2)
        self.queue.transition(2, S.RESUME_READY, application_folder_relpath='existing-2')
        worker = self.worker()
        self.run_tick(worker)
        self.assertEqual((self.queue.get(2).state, self.queue.get(2).codex_application_session_id), (S.RESUME_READY, None))

    def test_the_sheet_is_read_again_after_a_second(self):
        self.member_ready_to_start(2)
        self.assertEqual([item.job_id for item in self.pipeline.batch_items()], [2])
        sheet.qualify(self.f, 2)
        with patch('pipeline.autopilot.SHEET_READ_SECONDS', 0):
            self.assertEqual(self.pipeline.batch_items(), [])

    def test_a_job_an_applier_already_started_is_left_alone(self):
        sheet.qualify(self.f, 2)
        sheet.decide(self.f, 2, 'with_recruiter')
        self.member_ready_to_start(2)
        self.queue.transition(2, S.RESUME_READY)
        self.queue.transition(2, S.APPLYING, codex_application_session_id='claude:2')
        self.assertEqual([item.job_id for item in self.pipeline.batch_items()], [2])

    def test_a_stranded_job_with_a_decision_is_not_adopted(self):
        sheet.qualify(self.f, 1)
        sheet.decide(self.f, 1, 'with_recruiter')
        self.stranded(1)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.members(), [])
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        self.assertEqual([event['reason'] for event in self.events if event.get('event') == 'stranded_job_refused'],
                         ['it is on the recruiter sheet'])

    def test_a_selected_batch_still_lists_a_job_on_the_sheet(self):
        self.member_ready_to_start(2)
        sheet.qualify(self.f, 2)
        manifest = self.manifest()
        manifest.pop('continuous')
        selected = self.root / '.pipeline/batches/selected.json'
        selected.write_text(json.dumps(manifest))
        pipeline = BatchPipeline(self.config, selected)
        self.assertEqual([item.job_id for item in pipeline.batch_items()], [2])
        self.assertEqual(pipeline.prepare_existing_resumes(), [2])

    def test_a_read_failure_admits_nothing_and_reports_once_until_it_recovers(self):
        self.eligible = [2]
        self.break_sheet()
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.members(), self.queries, self.queue.get(2)), ([], 0, None))
        self.assertEqual(len(self.unavailable_events()), 1)
        self.repair_sheet()
        self.assertEqual(self.tick(), [2])
        self.assertIn('recruiter_sheet_available', [event.get('event') for event in self.events])
        self.break_sheet()
        self.tick()
        self.assertEqual(len(self.unavailable_events()), 2)

    def test_a_broken_config_also_admits_nothing(self):
        self.eligible = [2]
        (self.f.db.parent.parent / 'config.toml').write_text('[recruiter_sheet]\nunknown = 1\n')
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.queries, 0)
        self.assertEqual(len(self.unavailable_events()), 1)

    def test_a_read_failure_starts_no_member_until_the_sheet_is_back(self):
        self.member_ready_to_start(2)
        self.break_sheet()
        worker = self.worker()
        self.run_tick(worker)
        self.assertFalse(self.started(2))
        self.assertTrue(self.unavailable_events())
        self.repair_sheet()
        self.run_tick(worker)
        self.assertTrue(self.started(2))

    def test_a_read_failure_on_the_first_tick_delays_adoption_until_it_can_run_first(self):
        self.rebooted()
        self.member_ready_to_start(2)
        self.break_sheet()
        worker = self.worker()
        self.run_tick(worker)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        self.assertEqual(self.launched_while, [])
        self.repair_sheet()
        self.run_tick(worker)
        self.assertEqual(self.launched_while, [S.QUEUED], "the stranded job was adopted before the batch's own job started")
        self.assertIn(1, self.members())


class ResumeUpdateTests(StrandedFixture):
    """The user uploads a new resume between worker runs. The continuous batch keeps its manifest path, so a job's folder stays the same."""
    worker = StrandedWorkerOrderTests.worker
    settle = StrandedWorkerOrderTests.settle
    member_ready_to_start = StrandedWorkerOrderTests.member_ready_to_start
    PAGE = 'https://fixture.invalid/jobs/1/apply'
    NEW_PDF = b'%PDF-1.4 synthetic new resume'

    def setUp(self):
        super().setUp()
        self.member_ready_to_start(1)
        self.first_worker = self.worker()
        self.applying()
        self.folder = self.config.applications_root / self.queue.get(1).application_folder_relpath
        self.old_contract = (self.folder / 'resume-catalog.json').read_bytes()
        self.selected_old = self.tools().select_resume('fixture-original', 'Fixture resume fits')
        with (self.folder / 'jd.md').open('a') as stream:
            stream.write('\n- Team choice: Data Engineering\n')
        self.jd = (self.folder / 'jd.md').read_bytes()

    def applying(self):
        for _ in range(2):
            self.first_worker.tick()
            self.settle(self.first_worker)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)

    def tools(self):
        owner = self.queue.get(1).codex_application_session_id
        return ApplierTools(self.launcher.store, 1, owner, evidence_dir=self.config.applications_root / self.queue.get(1).application_folder_relpath,
                            jobfeed_db=self.config.jobfeed_db, materials_root=self.root)

    def end_session(self):
        record = self.launcher._record(self.queue.get(1).codex_application_session_id)
        next(agent for agent in self.fake.agents if agent['id'] == record['background_id'])['state'] = 'done'

    def park(self):
        ClaudeServices(self.pipeline, supervise_every=0, env_path=self.root / 'no-discord.env').tick()

    def upload_new_resume_and_restart(self):
        """What the user's upload and Service.step do between worker runs."""
        (self.root / 'fixture-new.pdf').write_bytes(self.NEW_PDF)
        (self.root / 'resume-catalog.json').write_text(json.dumps({'schema_version': 1, 'resumes': [
            {'id': 'new-resume', 'label': 'New resume', 'available': True, 'pdf': 'fixture-new.pdf'}]}))
        self.pipeline = BatchPipeline(self.config, autopilot.write_manifest(self.root))
        self.pipeline.initialize()
        self.queue = self.pipeline.queue
        self.pipeline.jobfeed.transport = self.results
        return self.worker(self.pipeline)

    def assert_next_applier_gets_the_new_resume(self, worker):
        for _ in range(2):
            worker.tick()
            self.settle(worker)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        contract = json.loads((self.folder / 'resume-catalog.json').read_text())
        self.assertEqual(contract['catalog'], self.manifest()['resume_catalog'])
        tools = self.tools()
        self.assertEqual(tools.list_resumes(), {'resumes': [{'resume_id': 'new-resume', 'label': 'New resume'}]})
        upload = tools.select_resume('new-resume', 'The new resume fits')['upload']
        self.assertEqual(Path(upload).read_bytes(), self.NEW_PDF)
        owner = self.queue.get(1).codex_application_session_id
        self.assertEqual(verify_selection(self.config, str(autopilot.manifest_path(self.root)), 1, owner)['resume_id'], 'new-resume')

    def requeue_cli(self, *args):
        from pipeline.cli import main
        out = io.StringIO()
        # Both patches stop the command reading this host: its real `claude agents` and any real `pipeline.cli watch`, which would refuse the requeue.
        with (patch.object(sys, 'argv', ['pipeline.cli', '--root', str(self.root), 'requeue', *args, '--reason', 'New resume']),
              patch('pipeline.requeue.ClaudeApplierLauncher', lambda root: self.launcher), patch('pipeline.requeue.watch_process', lambda: False), redirect_stdout(out)):
            code = main()
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def assert_prepared_on_the_new_contract(self, job_id, folder, old_contract, worker):
        self.events.clear()
        worker.tick()
        self.settle(worker)
        # The fixture's inline executor starts the Applier in the same tick, so the worker's own event shows Resume Ready.
        self.assertIn({'event': 'resume_ready', 'job_id': job_id}, self.events)
        self.assertEqual(self.queue.get(job_id).state, S.APPLYING)
        self.assertEqual(self.queue.get(job_id).application_folder_relpath, folder.name)
        contract = json.loads((folder / 'resume-catalog.json').read_text())
        self.assertEqual(contract['catalog'], self.manifest()['resume_catalog'])
        self.assertEqual([entry['id'] for entry in contract['catalog']['entries']], ['new-resume'])
        self.assertNotEqual((folder / 'resume-catalog.json').read_bytes(), old_contract)

    def assert_old_contract_kept(self):
        [history] = (self.folder / 'resume-catalogs').iterdir()
        self.assertEqual(history.read_bytes(), self.old_contract)

    def captcha_requeue(self):
        tools = self.tools()
        tools.report_blocker('captcha', 'Cloudflare asks to verify you are human.', page_url=self.PAGE)
        tools.record_tab_closed()
        self.end_session()
        self.park()
        item = self.queue.get(1)
        self.assertEqual((item.state, item.codex_application_session_id, item.application_folder_relpath, item.held), (S.QUEUED, None, None, True))

    def test_a_job_held_for_a_captcha_is_prepared_against_the_new_catalog_after_the_clear(self):
        self.captcha_requeue()
        self.assertFalse((self.folder / 'resume-source.json').exists())
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), self.old_contract, 'the folder still holds the contract of its first run')
        worker = self.upload_new_resume_and_restart()
        self.assertEqual(self.pipeline.process_completions(), [], 'a held job is not prepared')
        done = subprocess.run([sys.executable, '-m', 'pipeline.cli', '--root', str(self.root), 'captcha', 'clear', '1'],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_next_applier_gets_the_new_resume(worker)
        self.assert_old_contract_kept()
        self.assertEqual((self.folder / 'jd.md').read_bytes(), self.jd, 'the posting and what earlier runs added to it stay')
        [requeue_record] = (self.folder / 'resume-requeues').iterdir()
        self.assertEqual(json.loads(requeue_record.read_text())['resume_id'], 'fixture-original')

    def test_a_job_that_lost_its_tab_is_prepared_against_the_new_catalog(self):
        self.tools().report_blocker('tab_missing', 'My form tab is gone.')
        self.end_session()
        self.park()
        self.assertEqual((self.queue.get(1).state, self.queue.get(1).held), (S.QUEUED, False))
        self.assert_next_applier_gets_the_new_resume(self.upload_new_resume_and_restart())
        self.assert_old_contract_kept()

    def test_a_job_requeued_with_keep_batches_is_prepared_against_the_new_catalog(self):
        self.end_session()
        [result] = self.requeue_cli('1', '--keep-batches')
        self.assertEqual(result['status'], 'requeued')
        self.assertEqual(result['audit']['retired_owner'], self.store.requeues(1)[0]['retired_owner'])
        self.assertEqual(self.store.requeues(1)[0]['batches'], [])
        self.assert_prepared_on_the_new_contract(1, self.folder, self.old_contract, self.upload_new_resume_and_restart())
        self.assert_old_contract_kept()
        self.assertEqual((self.folder / 'jd.md').read_bytes(), self.jd)
        self.assert_next_applier_gets_the_new_resume(self.worker(self.pipeline))

    def test_a_resume_ready_job_requeued_with_keep_batches_is_prepared_against_the_new_catalog(self):
        self.member_ready_to_start(2)
        self.assertEqual(self.pipeline.process_completions(), [2])
        self.assertEqual(self.queue.get(2).state, S.RESUME_READY)
        folder = self.config.applications_root / self.queue.get(2).application_folder_relpath
        old_contract = (folder / 'resume-catalog.json').read_bytes()
        [result] = self.requeue_cli('2', '--keep-batches')
        self.assertEqual((result['status'], result['previous_state']), ('requeued', 'Resume Ready'))
        self.assert_prepared_on_the_new_contract(2, folder, old_contract, self.upload_new_resume_and_restart())
        [history] = (folder / 'resume-catalogs').iterdir()
        self.assertEqual(history.read_bytes(), old_contract)

    def test_a_job_requeued_without_the_flag_stays_out_of_its_batch(self):
        self.end_session()
        [result] = self.requeue_cli('1')
        self.assertEqual(result['status'], 'requeued')
        self.assertEqual(self.store.requeues(1)[0]['batches'], [str(autopilot.manifest_path(self.root)), str(self.old)])
        worker = self.upload_new_resume_and_restart()
        self.assertEqual(self.manifest()['suspended_job_ids'], [1])
        worker.tick()
        self.settle(worker)
        self.assertEqual(self.queue.get(1).state, S.QUEUED)
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), self.old_contract)

    def test_a_dry_run_tells_what_each_flag_does_to_each_batch(self):
        self.end_session()
        batch, old = str(autopilot.manifest_path(self.root)), str(self.old)
        earlier = self.root / '.pipeline/batches/earlier.json'
        earlier.write_text(json.dumps({'ordered_candidates': [{'id': 1}], 'suspended_job_ids': [1]}))
        files = [Path(batch), self.old, earlier]
        before = [path.read_bytes() for path in files]
        def batches(plan):
            return plan['release_from_batches'], plan['keep_in_batches'], plan['already_suspended_in_batches']
        [default] = self.requeue_cli('1', '--dry-run')
        self.assertEqual(batches(default['plan']), ([batch, old, str(earlier)], [], [str(earlier)]))
        [keep] = self.requeue_cli('1', '--dry-run', '--keep-batches')
        self.assertEqual(batches(keep['plan']), ([], [batch, old], [str(earlier)]))
        self.assertEqual(keep['status'], 'would_requeue')
        self.assertEqual([path.read_bytes() for path in files], before)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        [done] = self.requeue_cli('1', '--keep-batches')
        self.assertEqual(batches(done['plan']), batches(keep['plan']))
        self.assertEqual([path.read_bytes() for path in files], before, 'the real run did what its dry run said')

    def test_keep_batches_does_not_lift_a_suspension_an_earlier_requeue_left(self):
        self.end_session()
        self.requeue_cli('1')
        [again] = self.requeue_cli('1', '--keep-batches')
        batch, old = str(autopilot.manifest_path(self.root)), str(self.old)
        self.assertEqual((again['status'], again['already_suspended_in_batches']), ('already_queued', [batch, old]))
        self.assertEqual(self.manifest()['suspended_job_ids'], [1])

    def test_a_job_with_an_active_selection_keeps_its_frozen_contract(self):
        worker = self.upload_new_resume_and_restart()
        selection = (self.folder / 'resume-source.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'another catalog'):
            prepare_application(self.config, self.pipeline.jobfeed.get_job(1), self.pipeline.manifest_path, self.pipeline.resume_catalog)
        worker.tick()
        self.settle(worker)
        self.assertEqual(self.queue.get(1).state, S.APPLYING)
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), self.old_contract)
        self.assertEqual((self.folder / 'resume-source.json').read_bytes(), selection)
        self.assertFalse((self.folder / 'resume-catalogs').exists())

    def test_a_crash_during_the_rebind_converges_on_the_next_pass(self):
        self.captcha_requeue()
        self.queue.clear_hold(1, 'user via test')
        self.upload_new_resume_and_restart()
        real = publish

        def crash_before_contract(path, data):
            if Path(path).name == 'resume-catalog.json':
                raise OSError('simulated crash')
            real(path, data)
        with patch('pipeline.resume_catalog.publish', crash_before_contract), self.assertRaisesRegex(OSError, 'simulated'):
            self.pipeline.process_completions()
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), self.old_contract)
        self.assert_old_contract_kept()
        self.assertEqual(self.queue.get(1).state, S.QUEUED)

        self.assertEqual(self.pipeline.process_completions(), [1])
        self.assert_old_contract_kept()
        contract = (self.folder / 'resume-catalog.json').read_bytes()
        self.assertEqual(json.loads(contract)['catalog'], self.manifest()['resume_catalog'])
        self.assertEqual((self.folder / 'jd.md').read_bytes(), self.jd)

        prepare_application(self.config, self.pipeline.jobfeed.get_job(1), self.pipeline.manifest_path, self.pipeline.resume_catalog)
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), contract, 'a crash before the queue write only repeats the pass')
        self.assert_old_contract_kept()

    def test_a_retried_job_with_the_same_catalog_keeps_its_contract_and_adds_no_history(self):
        self.captcha_requeue()
        self.queue.clear_hold(1, 'user via test')
        self.assertEqual(self.pipeline.process_completions(), [1])
        self.assertEqual((self.folder / 'resume-catalog.json').read_bytes(), self.old_contract)
        self.assertFalse((self.folder / 'resume-catalogs').exists())


class AutopilotCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = RuntimeFixture(temp.name).root

    def cli(self, *args):
        from pipeline.cli import main
        out = io.StringIO()
        with patch.object(sys, 'argv', ['pipeline.cli', '--root', str(self.root), 'autopilot', *args]), redirect_stdout(out):
            code = main()
        return code, json.loads(out.getvalue())

    def test_on_off_and_status_record_who_changed_it(self):
        self.assertEqual(self.cli('status')[1]['enabled'], False)
        code, record = self.cli('on')
        self.assertEqual((code, record['enabled'], record['changed_by']), (0, True, 'user via pipeline.cli autopilot'))
        self.assertEqual(self.cli('on')[1]['changed_at'], record['changed_at'])
        self.assertEqual(self.cli('status')[1], record)
        code, record = self.cli('off', '--by', 'agent on request')
        self.assertEqual((code, record['enabled'], record['changed_by']), (0, False, 'agent on request'))
        self.assertEqual(autopilot.main(['--root', str(self.root), 'enabled']), 1)
        self.cli('on')
        self.assertEqual(autopilot.main(['--root', str(self.root), 'enabled']), 0)

    def test_by_cannot_name_who_turns_it_on(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.cli('on', '--by', 'agent')
        self.assertFalse(ApplierRuntime(self.root).autopilot()['enabled'])

    def test_unreadable_record_is_off(self):
        ApplierRuntime(self.root).set_control('autopilot', '{not json')
        self.assertFalse(ApplierRuntime(self.root).autopilot()['enabled'])


class Process:
    def __init__(self, code=0):
        self.pid, self.code, self.signals = 616161, code, []

    def poll(self):
        return None

    def wait(self):
        return self.code

    def send_signal(self, number):
        self.signals.append(number)


class AutopilotServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = RuntimeFixture(temp.name)
        self.root = self.f.root
        (self.root / 'application-profile.md').write_text('Synthetic confirmed profile.\n')
        bin_dir = Path(temp.name) / 'bin'
        bin_dir.mkdir()
        for name in ('claude', 'jobfeed'):
            (bin_dir / name).write_text('#!/bin/sh\nexit 1\n')
            (bin_dir / name).chmod(0o755)
        discord = Path(temp.name) / 'discord.env'
        discord.write_text('DISCORD_BOT_TOKEN=synthetic\n')
        claude_config = Path(temp.name) / 'claude.json'
        claude_config.write_text(json.dumps({'projects': {str(self.root.parent): {'hasTrustDialogAccepted': True}}}))
        self.spawned, self.codes = [], []
        self.host = Host(spawn=self.spawn, alive=lambda pid, manifest: False, python=str(self.root / '.venv/bin/python'),
                         env={'PATH': str(bin_dir)}, discord_env=discord, claude_config=claude_config)
        self.service = autopilot.Service(self.root, self.host, poll=0, log=lambda line: None)

    def spawn(self, argv, **kwargs):
        self.spawned.append(argv)
        return Process(self.codes.pop(0) if self.codes else 0)

    def test_off_starts_no_worker(self):
        self.assertEqual(self.service.step(), 'off')
        self.assertEqual(self.spawned, [])
        self.assertFalse(autopilot.manifest_path(self.root).exists())

    def test_on_starts_the_continuous_claude_worker_and_records_its_launch(self):
        ApplierRuntime(self.root).set_autopilot(True, actor='user via test', source='test')
        self.assertEqual(self.service.step(), 'worker_exit 0')
        argv = self.spawned[0]
        self.assertEqual(argv[argv.index('--batch') + 1], str(autopilot.manifest_path(self.root)))
        self.assertEqual(argv[argv.index('--application-workers') + 1], '4')
        self.assertIn('--worker-child', argv)
        self.assertEqual(json.loads(autopilot.launch_path(self.root).read_text())['pid'], 616161)
        self.assertTrue(json.loads(autopilot.manifest_path(self.root).read_text())['continuous'])

    def test_waits_while_another_worker_holds_the_lock(self):
        import fcntl
        ApplierRuntime(self.root).set_autopilot(True, actor='user via test', source='test')
        with (self.root / '.pipeline/worker-progress.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.service.step(), 'another_worker')
        self.assertEqual(self.spawned, [])

    def test_a_draining_worker_gets_only_one_sigint(self):
        import dataclasses
        from pipeline.local_state import now, private_json
        ApplierRuntime(self.root).set_autopilot(True, actor='user via test', source='test')
        autopilot.write_manifest(self.root)
        private_json(autopilot.launch_path(self.root), {'argv': autopilot.worker_command(self.root, 'python'), 'pid': 717171, 'started_at': now()}, self.root)
        interrupted = []
        host = dataclasses.replace(self.host, alive=lambda pid, manifest: pid == 717171, interrupt=interrupted.append, sleep=lambda s: None)
        service = autopilot.Service(self.root, host, poll=0, log=lambda line: None)
        self.assertEqual([service.step(), service.step()], ['another_worker', 'another_worker'])
        self.assertEqual((interrupted, self.spawned), ([717171], []))

    def test_repeated_worker_failures_stop_the_service(self):
        ApplierRuntime(self.root).set_autopilot(True, actor='user via test', source='test')
        self.codes = [1, 1, 1]
        self.assertEqual(self.service.serve(), autopilot.EXHAUSTED)
        self.assertEqual(len(self.spawned), 3)

    def test_stop_before_the_first_step_starts_nothing(self):
        self.service.stop()
        self.assertEqual(self.service.serve(), 0)
        self.assertEqual(self.spawned, [])


if __name__ == '__main__':
    unittest.main()
