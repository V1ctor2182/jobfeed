"""The user's requeue command for jobs a dead worker stranded; synthetic temp workspace, fake Claude CLI."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from test_claude_applier import FakeClaude
from pipeline.applier_runtime import ApplierRuntime
from pipeline.cancellation import CancellationStore
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.claude_dashboard_start import claimed
from pipeline.claude_dispatch import start_claude_application
from pipeline.config import PipelineConfig
from pipeline.dashboard_submission import DashboardSubmissions
from pipeline.lifecycle import ApplicationState
from pipeline.requeue import boot_time, process_start, requeue
from pipeline.store import ApplicationQueue
from pipeline.submission_reservation import reservations

REASON = 'Power outage stopped the worker on 2026-10-28'
JOBS = range(1, 13)
BOOT = 1793580064
BEFORE_BOOT, AFTER_BOOT = (BOOT - 6 * 3600) * 1000, (BOOT + 60) * 1000


class RequeueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = base / 'resume'
        (self.root / '.pipeline/batches').mkdir(parents=True)
        self.config = PipelineConfig.discover(self.root)
        self.config.jobfeed_db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY, url TEXT, company TEXT); CREATE TABLE applied(job_id INTEGER);')
            db.executemany('INSERT INTO jobs(id,url) VALUES(?,?)', [(i, f'https://board.example.test/{i}') for i in JOBS])
        self.queue = ApplicationQueue(self.config.jobfeed_db)
        self.queue.initialize()
        self.old = self.manifest('old', list(JOBS))
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        patcher = patch('pipeline.claude_dispatch.launcher_for', return_value=self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = ApplierRuntime(self.root)
        self.busy = False
        # The Claude background service is up unless a test says otherwise.
        self.daemon = base / 'daemon.status.json'
        self.daemon_status(written_at=AFTER_BOOT)
        self.processes = {4242: '22943599'}

    def daemon_status(self, **fields):
        status = {'supervisorPid': 4242, 'supervisorProcStart': '22943599', 'writtenAt': fields.pop('written_at', None)} | fields
        self.daemon.write_text(json.dumps({k: v for k, v in status.items() if v is not None}))

    def manifest(self, name, ids):
        path = self.root / '.pipeline/batches' / f'dashboard-claude-{name}.json'
        path.write_text(json.dumps({'application_runtime': 'claude', 'ordered_candidates': [{'id': i} for i in ids]}))
        return path

    def insert(self, job_id, state, owner=None, folder=None, **columns):
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('INSERT INTO application_queue(job_id,state,codex_application_session_id,application_folder_relpath,manual_priority) VALUES(?,?,?,?,1)',
                       (job_id, state, owner, folder))
            for column, value in columns.items():
                db.execute(f'UPDATE application_queue SET {column}=? WHERE job_id=?', (value, job_id))

    def row(self, job_id):
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db:
            db.row_factory = sqlite3.Row
            found = db.execute('SELECT * FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
        return dict(found) if found else None

    def start(self, job_id, manifest, folder):
        """What the worker does for a Resume Ready job in a Claude batch."""
        (self.config.applications_root / folder).mkdir(parents=True, exist_ok=True)
        pipeline = SimpleNamespace(
            config=self.config, manifest_path=manifest, candidate_snapshots={},
            applications=SimpleNamespace(execution_scope='unused', execution_gate=CancellationStore(self.root / '.pipeline/execution-gates.sqlite3')),
            queue=self.queue)
        job = SimpleNamespace(id=job_id, url=f'https://board.example.test/{job_id}')
        return start_claude_application(pipeline, self.queue.get(job_id), job, self.config.applications_root / folder)

    def stranded_applier(self, job_id=1):
        """An Applier that asked a question, filled a field, and then lost its session in an outage."""
        self.insert(job_id, 'Resume Ready', folder=f'existing-old-{job_id}')
        owner = self.start(job_id, self.old, f'existing-old-{job_id}')['launch_id']
        self.store.register_tab(job_id, owner, 41, 'https://acme.example.test/apply')
        self.store.readback(job_id, owner, 'Name', 'Ada')
        answered = self.store.ask(job_id, owner, 'question', 'Are you authorized to work in the U.S.?', field='auth')
        self.store.reply(answered, 'Yes', source_kind='discord', source_id=f'm1-{job_id}', sender_id='400')
        question = self.store.ask(job_id, owner, 'question', 'What is your start date?', field='start_date')
        self.fake.agents.clear()
        return owner, question

    def requeue(self, *ids, dry_run=False):
        return requeue(self.config, list(ids), REASON, dry_run=dry_run, worker_busy=lambda root: self.busy, inventory=self.launcher.agents,
                       service_status=self.daemon, start_of=self.processes.get, booted_at=lambda: BOOT)

    def settled_attempt(self, job_id=1):
        """Job 13 after the outage: one Dashboard-approved Submit that stayed on the form, and its Applier session gone."""
        owner, _ = self.stranded_applier(job_id)
        event = str(uuid.uuid4())
        self.store.authorize_submit(job_id, source_kind='dashboard', event_id=event)
        self.assertEqual(self.store.request_submit(job_id, owner), (True, 'authorized'))
        self.store.record_submit_outcome(job_id, owner, 'not_submitted', 'The page stayed on the form: "This field is required". No confirmation page.')
        self.ledger(event, job_id, 'blocked')
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('UPDATE application_queue SET submit_authorization=? WHERE job_id=?',
                       (json.dumps({'authorized': True, 'job_id': job_id, 'luna_session_id': owner, 'source': {'event_id': event}}), job_id))
        return owner, event

    def ledger(self, request_id, job_id, state):
        with DashboardSubmissions(self.config).db() as db:
            db.execute("INSERT INTO requests(id,job,owner,state,preview,prompt,mode,created_at) VALUES(?,?,'o',?,'{}','Submit now.','claude','2026-10-28')",
                       (request_id, job_id, state))

    def requests(self, job_id):
        with DashboardSubmissions(self.config).db() as db:
            return {row['id']: row['state'] for row in db.execute('SELECT id,state FROM requests WHERE job=?', (job_id,))}

    def submit_rows(self, job_id):
        with closing(sqlite3.connect(self.store.path)) as db:
            return [db.execute(f'SELECT count(*) FROM {table} WHERE job_id=?', (job_id,)).fetchone()[0] for table in ('applier_submit_auth', 'applier_submits')]

    def assert_requeued_clean(self, job_id, event):
        self.assertEqual((self.row(job_id)['state'], self.row(job_id)['submit_authorization']), ('Queued', None))
        self.assertEqual(self.submit_rows(job_id), [0, 0])
        self.assertEqual(self.requests(job_id), {event: 'not_submitted'})
        self.assertEqual([row for row in reservations(self.root) if row['job'] == job_id], [])
        [record] = self.store.requeues(job_id)
        self.assertEqual(([r['event_id'] for r in record['retired']['applier_submit_auth']], record['retired']['applier_submits'][0]['state']),
                         ([event], 'not_submitted'))

    def test_a_resume_ready_job_goes_back_to_the_queue_and_dashboard_start_accepts_it(self):
        self.insert(2, 'Resume Ready', folder='existing-old-2', retry_count=3, next_retry_at='2026-10-28T09:00:00+00:00')
        [result] = self.requeue(2)
        self.assertEqual(result['status'], 'requeued')
        self.assertEqual(self.row(2), {'job_id': 2, 'state': 'Queued', 'retry_count': 0, 'next_retry_at': None, 'application_folder_relpath': None,
                                       'codex_application_session_id': None, 'claude_session_id': None, 'claude_session_url': None,
                                       'submit_authorization': None, 'hold': None, 'manual_priority': 1})
        self.assertEqual(list(claimed(self.config.jobfeed_db, [{'source_job_id': 2}])), [])
        self.assertEqual(json.loads(self.old.read_text())['suspended_job_ids'], [2])
        [audit] = self.store.requeues(2)
        self.assertEqual((audit['actor'], audit['reason'], audit['previous_state'], audit['previous_folder'], audit['retired_owner'], audit['batches']),
                         ('user via pipeline.cli requeue', REASON, 'Resume Ready', 'existing-old-2', None, [str(self.old)]))

    def test_an_applying_job_retires_its_ended_applier_and_a_new_applier_starts_clean(self):
        owner, question = self.stranded_applier()
        folder = self.config.applications_root / 'existing-old-1'
        receipt = {'schema_version': 2, 'kind': 'selected_existing', 'job_id': 1, 'owner': owner, 'resume_id': 'upload-1'}
        (folder / 'resume-source.json').write_text(json.dumps(receipt))
        [result] = self.requeue(1)
        self.assertEqual((result['status'], result['audit']['retired_owner']), ('requeued', owner))
        self.assertFalse((folder / 'resume-source.json').exists(), 'the next Applier selects its resume again')
        self.assertEqual(json.loads((folder / 'resume-requeues' / f"{result['audit']['id']}.json").read_text()), receipt)
        self.assertEqual((self.row(1)['state'], self.row(1)['codex_application_session_id']), ('Queued', None))
        self.assertEqual(self.store.request(question)['state'], 'retired')
        with self.assertRaisesRegex(ValueError, 'went back to the queue'):
            self.store.reply(question, 'Monday', source_kind='discord', source_id='m2', sender_id='400')
        self.assertIsNone(self.launcher.launch_for(1))
        self.assertTrue((self.config.applications_root / 'existing-old-1/claude-applier').is_dir())
        retired = self.store.requeues(1)[0]['retired']
        self.assertEqual([row['value'] for row in retired['applier_readbacks']], ['Ada'])
        self.assertEqual(retired['applier_launches'][0]['launch_id'], owner)
        self.assertEqual(self.store.latest_reply(retired['applier_requests'][0])['text'], 'Yes')

        new = self.manifest('new', [1])
        self.queue.transition(1, ApplicationState.RESUME_READY, application_folder_relpath='existing-new-1')
        started = self.start(1, new, 'existing-new-1')
        self.assertEqual(started['status'], 'claude_applier_started')
        self.assertNotEqual(started['launch_id'], owner)
        self.assertEqual(self.store.filled_form(1), ([], None))
        self.store.register_tab(1, started['launch_id'], 41, 'https://acme.example.test/apply')
        self.assertNotEqual(self.store.ask(1, started['launch_id'], 'question', 'What is your start date?', field='start_date'), question)

    def test_refusals_change_nothing(self):
        (self.config.applications_root / 'existing-old-3').mkdir(parents=True)
        (self.config.applications_root / 'existing-old-3/submission-receipt.json').write_text('{}')
        self.insert(2, 'Resume Ready', folder='existing-old-2', submit_authorization='{"authorized": true}')
        self.insert(3, 'Resume Ready', folder='existing-old-3')
        self.insert(4, 'Cancelled', folder='existing-old-4')
        self.insert(5, 'Submitted', folder='existing-old-5')
        before = {i: self.row(i) for i in range(2, 6)}
        reasons = {r['job_id']: (r['status'], r['reason']) for r in self.requeue(2, 3, 4, 5)}
        self.assertIn('submit authorization in the queue', reasons[2][1])
        self.assertIn('submission-receipt.json', reasons[3][1])
        self.assertIn('it is Cancelled', reasons[4][1])
        self.assertIn('it is Submitted', reasons[5][1])
        self.assertEqual({status for status, _ in reasons.values()}, {'refused'})
        self.assertEqual({i: self.row(i) for i in range(2, 6)}, before)
        self.assertNotIn('suspended_job_ids', json.loads(self.old.read_text()))

    def test_a_submit_record_a_running_worker_or_a_live_applier_refuses(self):
        owner, question = self.stranded_applier()
        self.busy = True
        self.assertIn('worker is running', self.requeue(1)[0]['reason'])
        self.busy = False
        self.fake.agents.append({'id': 'x', 'sessionId': 'x-1', 'name': f'applier-1-{owner[:8]}', 'cwd': str(self.config.applications_root / 'existing-old-1/claude-applier'), 'state': 'blocked'})
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.fake.agents.clear()
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, owner)
        self.assertIn('applier_submits is requested', self.requeue(1)[0]['reason'])
        self.assertEqual((self.row(1)['state'], self.store.request(question)['state']), ('Applying', 'open'))
        self.assertEqual(self.launcher.launch_for(1), owner)

    def test_a_session_listed_without_a_process_since_before_this_boot_has_ended(self):
        # 2026-11-18: six jobs could not be requeued while `claude agents` listed their dead sessions as blocked.
        owner, _ = self.stranded_applier()
        listed = {'id': 'x', 'sessionId': 'x-1', 'name': f'applier-1-{owner[:8]}', 'cwd': str(self.config.applications_root / 'existing-old-1/claude-applier'),
                  'state': 'blocked'}
        self.fake.agents.append(dict(listed, startedAt=AFTER_BOOT))
        self.assertIn('still running', self.requeue(1)[0]['reason'], 'one started in this boot may just be starting')
        self.fake.agents[:] = [dict(listed, startedAt=AFTER_BOOT, pid=4242)]
        self.assertIn('still running', self.requeue(1)[0]['reason'], 'and one with a process runs')
        self.fake.agents[:] = [dict(listed, startedAt=BEFORE_BOOT)]
        self.assertEqual(self.requeue(1)[0]['status'], 'requeued')

    def test_a_job_applied_in_jobfeed_is_refused(self):
        self.insert(2, 'Resume Ready', folder='existing-old-2')
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('INSERT INTO applied VALUES(2)')
        self.assertIn('applied in Jobfeed', self.requeue(2)[0]['reason'])

    def test_running_it_again_reports_already_queued_with_one_record(self):
        self.stranded_applier()
        self.assertEqual(self.requeue(1)[0]['status'], 'requeued')
        again = self.requeue(1)[0]
        self.assertEqual((again['status'], len(again['requeues'])), ('already_queued', 1))

    def test_a_requeue_that_stopped_after_retiring_the_applier_finishes_on_the_next_run(self):
        owner, _ = self.stranded_applier()
        self.store.requeue(1, 'Applying', 'existing-old-1', owner, [str(self.old)], actor='user via pipeline.cli requeue', reason=REASON)
        self.assertEqual(self.row(1)['state'], 'Applying')
        self.assertEqual(self.requeue(1)[0]['status'], 'requeued')
        self.assertEqual(self.row(1)['state'], 'Queued')
        self.assertEqual(len(self.store.requeues(1)), 1)

    def test_a_dry_run_reports_the_plan_and_writes_nothing(self):
        owner, question = self.stranded_applier()
        manifest = self.old.read_bytes()
        [result] = self.requeue(1, dry_run=True)
        self.assertEqual(result['status'], 'would_requeue')
        self.assertEqual(result['plan']['retire_applier']['owner'], owner)
        self.assertEqual(result['plan']['release_from_batches'], [str(self.old)])
        self.assertEqual((self.row(1)['state'], self.store.request(question)['state']), ('Applying', 'open'))
        self.assertEqual((self.old.read_bytes(), self.store.requeues(1), self.launcher.launch_for(1)), (manifest, [], owner))

    def test_the_cli_needs_a_reason_and_exits_nonzero_on_a_refusal(self):
        self.insert(4, 'Cancelled')
        command = [sys.executable, '-m', 'pipeline.cli', '--root', str(self.root), 'requeue', '4', '--dry-run']
        cwd = Path(__file__).resolve().parents[1]
        self.assertEqual(subprocess.run(command, cwd=cwd, capture_output=True).returncode, 2)
        done = subprocess.run([*command, '--reason', REASON], cwd=cwd, capture_output=True, text=True)
        self.assertEqual((done.returncode, json.loads(done.stdout)[0]['status']), (1, 'refused'))


    def test_a_submit_attempt_that_sent_nothing_is_retired_and_the_job_goes_back_unapproved(self):
        owner, event = self.settled_attempt()
        self.assertEqual([row['id'] for row in reservations(self.root)], [event])
        [result] = self.requeue(1)
        self.assertEqual((result['status'], result['submit_history'], result['audit']['retired_owner']), ('requeued', 'settled_unsent', owner))
        self.assert_requeued_clean(1, event)
        self.assertIsNone(self.launcher.launch_for(1))
        self.assertEqual(self.requeue(1)[0]['status'], 'already_queued')

    def test_a_dry_run_of_a_settled_attempt_writes_nothing(self):
        _, event = self.settled_attempt()
        before = (self.row(1), self.submit_rows(1), self.requests(1))
        [result] = self.requeue(1, dry_run=True)
        self.assertEqual((result['status'], result['plan']['retire_unsent_submit_attempt']['dashboard_requests_marked_not_submitted']), ('would_requeue', [event]))
        self.assertEqual((self.row(1), self.submit_rows(1), self.requests(1)), before)

    def test_submit_records_that_are_not_one_settled_unsent_attempt_still_refuse(self):
        def unused_auth_during(state):
            def change(job, owner, event):
                self.store.authorize_submit(job, source_kind='dashboard', event_id=f'later-approval-{state}')
                outcome(state)(job, owner, event)
            return change

        def outcome(state):
            def change(job, owner, event):
                with closing(sqlite3.connect(self.store.path)) as db, db:
                    db.execute('UPDATE applier_submits SET state=? WHERE job_id=?', (state, job))
            return change

        def ledger_state(state):
            def change(job, owner, event):
                with DashboardSubmissions(self.config).db() as db:
                    db.execute('UPDATE requests SET state=? WHERE id=?', (state, event))
            return change

        def outbox(job, owner, event):
            with closing(sqlite3.connect(self.store.path)) as db, db:
                db.execute("INSERT INTO applier_outbox(job_id,kind,state,created_at) VALUES(?,'jobfeed_sync','pending','2026-10-28')", (job,))

        variants = {
            'unused approval during a requested attempt': (unused_auth_during('requested'), 'applier_submit_auth later-approval-requested is unused'),
            'unused approval after an unknown attempt': (unused_auth_during('unknown'), 'applier_submit_auth later-approval-unknown is unused'),
            'submitted': (outcome('submitted'), 'applier_submits is submitted'),
            'unknown': (outcome('unknown'), 'applier_submits is unknown'),
            'requested': (outcome('requested'), 'applier_submits is requested'),
            'ledger queued': (ledger_state('queued'), 'is queued'),
            'ledger sync_pending': (ledger_state('sync_pending'), 'is sync_pending'),
            'ledger submitted': (ledger_state('submitted'), 'is submitted'),
            'blocked request of another approval': (lambda job, owner, event: self.ledger(str(uuid.uuid4()), job, 'turn_ended'), 'is turn_ended'),
            'outbox': (outbox, 'applier_outbox'),
            'skip': (lambda job, owner, event: self.store.ignore(job, source_id='m9', sender_id='400'), 'applier_skips'),
        }
        for job, (name, (change, reason)) in enumerate(variants.items(), start=1):
            with self.subTest(name):
                owner, event = self.settled_attempt(job)
                change(job, owner, event)
                before = (self.row(job), self.submit_rows(job), self.requests(job))
                [result] = self.requeue(job)
                self.assertEqual(result['status'], 'refused')
                self.assertIn(reason, result['reason'])
                self.assertEqual((self.row(job), self.submit_rows(job), self.requests(job), self.store.requeues(job)), (*before, []))

    def unknown_attempt(self, job_id=1):
        """An attempt that stopped on an emailed code: recorded unknown, its Dashboard request still queued."""
        owner, event = self.settled_attempt(job_id)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE applier_submits SET state='unknown' WHERE job_id=?", (job_id,))
        with DashboardSubmissions(self.config).db() as db:
            db.execute("UPDATE requests SET state='queued' WHERE id=?", (event,))
        return owner, event

    def test_an_unknown_attempt_goes_back_only_when_the_user_states_it_sent_nothing(self):
        owner, event = self.unknown_attempt()
        self.assertIn('applier_submits is unknown', self.requeue(1)[0]['reason'])
        [result] = requeue(self.config, [1], REASON, worker_busy=lambda root: False, inventory=self.launcher.agents, service_status=self.daemon,
                           start_of=self.processes.get, booted_at=lambda: BOOT, unknown_sent_nothing=True)
        self.assertEqual((result['status'], result['audit']['retired_owner']), ('requeued', owner))
        self.assertEqual((self.row(1)['state'], self.row(1)['submit_authorization'], self.submit_rows(1)), ('Queued', None, [0, 0]))
        self.assertEqual(self.requests(1), {event: 'not_submitted'})
        self.assertEqual(self.store.requeues(1)[0]['retired']['applier_submits'][0]['state'], 'unknown')

    def test_under_the_retry_rule_only_a_receipt_without_an_acknowledgement_is_set_aside(self):
        # 2026-11-18: Oracle's "Are You Still With Us?" dialog, saved as a receipt, kept job 6842 from ever moving.
        from pipeline import receipts
        owner, event = self.unknown_attempt()
        (self.config.applications_root / 'existing-old-1/submission-receipt.json').write_text('{}')

        def retried():
            return requeue(self.config, [1], REASON, worker_busy=lambda root: False, inventory=self.launcher.agents, service_status=self.daemon,
                           start_of=self.processes.get, booted_at=lambda: BOOT, retry_attempt=True)[0]
        for judged in (receipts.ACCEPTED, None):
            with self.subTest(judged), patch('pipeline.receipts.verdict', return_value=judged):
                self.assertIn('submission-receipt.json', retried()['reason'])
        with patch('pipeline.receipts.verdict', return_value=receipts.UNACKNOWLEDGED):
            self.assertIn('submission-receipt.json', self.requeue(1)[0]['reason'], 'without the retry rule the file still counts')
            self.assertEqual(retried()['status'], 'requeued')

    def test_the_users_statement_never_covers_a_submitted_or_open_attempt(self):
        for job, state in enumerate(('submitted', 'requested'), start=1):
            with self.subTest(state):
                self.unknown_attempt(job)
                with closing(sqlite3.connect(self.store.path)) as db, db:
                    db.execute('UPDATE applier_submits SET state=? WHERE job_id=?', (state, job))
                [result] = requeue(self.config, [job], REASON, worker_busy=lambda root: False, inventory=self.launcher.agents,
                                   service_status=self.daemon, start_of=self.processes.get, booted_at=lambda: BOOT, unknown_sent_nothing=True)
                self.assertEqual(result['status'], 'refused')
                self.assertIn(f'applier_submits is {state}', result['reason'])

    def test_a_queue_authorization_from_another_approval_still_refuses(self):
        self.settled_attempt()
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('UPDATE application_queue SET submit_authorization=? WHERE job_id=1', (json.dumps({'authorized': True, 'source': {'event_id': 'other'}}),))
        self.assertIn('submit authorization in the queue', self.requeue(1)[0]['reason'])

    def test_a_settled_attempt_requeue_that_stopped_partway_finishes_on_the_next_run(self):
        for job, settle_ledger in ((1, False), (2, True)):
            with self.subTest(after='ledger' if settle_ledger else 'runtime'):
                owner, event = self.settled_attempt(job)
                self.store.requeue(job, 'Applying', f'existing-old-{job}', owner, [str(self.old)], actor='user via pipeline.cli requeue', reason=REASON,
                                   settled_auth={event})
                if settle_ledger:
                    DashboardSubmissions(self.config).settle_requeued(job, [event], REASON)
                self.assertEqual(self.row(job)['state'], 'Applying')
                self.assertEqual(self.requeue(job)[0]['status'], 'requeued')
                self.assert_requeued_clean(job, event)
                self.assertEqual(self.requeue(job)[0]['status'], 'already_queued')

    def working_session(self):
        owner, _ = self.stranded_applier()
        self.fake.agents.append({'id': 'x', 'sessionId': 'x-1', 'name': f'applier-1-{owner[:8]}', 'cwd': str(self.config.applications_root / 'existing-old-1/claude-applier'), 'state': 'working'})

    def test_a_session_the_cli_still_reports_working_has_ended_once_the_background_service_is_provably_down(self):
        self.working_session()
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.daemon.unlink()
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.daemon.write_text('{"supervisorPid": 4242')
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.daemon_status(written_at=BEFORE_BOOT)
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.processes[4242] = '7311930'
        [reused] = self.requeue(1, dry_run=True)
        self.assertEqual(reused['status'], 'would_requeue')
        self.assertIn('now belongs to a process started at 7311930', reused['applier_session'])
        self.assertIn('before this boot at 2026-11-02T00:41:04+00:00', reused['applier_session'])
        self.fake.agents.append(dict(self.fake.agents[0], state='paused'))
        self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.fake.agents.pop()
        del self.processes[4242]
        [result] = self.requeue(1)
        self.assertEqual(result['status'], 'requeued')
        self.assertIn('background service is down: its supervisor pid 4242 is not running', result['applier_session'])
        self.assertIn('last written at 2026-11-01T18:41:04+00:00, before this boot', result['applier_session'])

    def test_a_dead_supervisor_is_not_proof_unless_its_status_predates_this_boot(self):
        self.working_session()
        del self.processes[4242]
        statuses = {'written after this boot': {'written_at': AFTER_BOOT}, 'no writtenAt': {}, 'writtenAt not a number': {'written_at': str(BEFORE_BOOT)}}
        for name, status in statuses.items():
            with self.subTest(name):
                self.daemon_status(**status)
                self.assertIn('still running', self.requeue(1)[0]['reason'])
        self.daemon_status(written_at=BEFORE_BOOT)
        def unreadable():
            raise OSError('/proc/stat is unreadable')
        self.assertIn('still running', requeue(self.config, [1], REASON, worker_busy=lambda root: False, inventory=self.launcher.agents,
                                               service_status=self.daemon, start_of=self.processes.get, booted_at=unreadable)[0]['reason'])
        self.assertEqual(self.row(1)['state'], 'Applying')
        self.assertEqual(self.requeue(1)[0]['status'], 'requeued')

    def started_with_tab(self, name, folder):
        """A new Applier for job 1 in its own batch, with its form tab registered."""
        manifest = self.root / f'.pipeline/batches/dashboard-claude-{name}.json'
        manifest.write_text(json.dumps({'application_runtime': 'claude', 'ordered_candidates': [{'id': 1, 'company': 'Acme', 'title': 'Engineer'}]}))
        owner = self.start(1, manifest, folder)['launch_id']
        self.store.register_tab(1, owner, 41, 'https://acme.example.test/apply')
        return manifest, owner

    def confirm(self, manifest, owner):
        """The user's Dashboard Submit for job 1, as the Dashboard runs it. Returns the request ID."""
        service = DashboardSubmissions(self.config)
        with patch('pipeline.dashboard_submission.selected_materials', return_value={'job_id': 1, 'scope': str(manifest), 'owner': owner, 'resume_id': 'r'}), \
                patch('pipeline.claude_applier.ClaudeApplierLauncher', side_effect=lambda root: self.launcher):
            preview = service.preview(scope=str(manifest), job_id=1)
            self.assertTrue(preview['eligible'], preview.get('reason'))
            request = str(uuid.uuid4())
            self.assertEqual(service.confirm(request_id=request, expected_preview=preview, confirmed=True)['state'], 'queued')
        return request

    def test_after_a_requeue_the_job_can_be_confirmed_for_submit_again(self):
        owner, event = self.settled_attempt()
        self.assertEqual(self.requeue(1)[0]['status'], 'requeued')
        self.queue.transition(1, ApplicationState.RESUME_READY, application_folder_relpath='existing-new-1')
        new, started = self.started_with_tab('new', 'existing-new-1')
        request = self.confirm(new, started)
        self.assertEqual(self.requests(1), {event: 'not_submitted', request: 'queued'})
        self.assertEqual(self.store.request_submit(1, started), (True, 'authorized'))

    def lost_tab_after_approval(self):
        """Job 742: the user confirmed Submit, then the Applier woke to find its tab gone and reported it without calling request_submit."""
        self.insert(1, 'Resume Ready', folder='existing-old-1')
        manifest, owner = self.started_with_tab('old-742', 'existing-old-1')
        request = self.confirm(manifest, owner)
        self.store.mark_tabs_missing(1, owner)
        self.store.ask(1, owner, 'blocker', 'My form tab is gone', blocker_type='tab_missing')
        self.fake.agents.clear()
        return owner, request

    def test_an_approval_the_applier_never_spent_is_withdrawn_and_a_new_applier_is_approved_fresh(self):
        owner, request = self.lost_tab_after_approval()
        self.assertEqual(json.loads(self.row(1)['submit_authorization'])['source']['event_id'], request)
        self.assertEqual([row['id'] for row in reservations(self.root)], [request])
        [plan] = self.requeue(1, dry_run=True)
        self.assertEqual((plan['status'], plan['submit_history'], plan['plan']['retire_unsent_submit_attempt']['unspent_approvals_withdrawn']),
                         ('would_requeue', 'unspent', [request]))
        [result] = self.requeue(1)
        self.assertEqual((result['status'], result['submit_history'], result['audit']['retired_owner']), ('requeued', 'unspent', owner))
        self.assertEqual((self.row(1)['state'], self.row(1)['submit_authorization']), ('Queued', None))
        self.assertEqual(self.submit_rows(1), [0, 0])
        self.assertEqual(self.requests(1), {request: 'not_submitted'})
        self.assertIn('Nothing was sent', DashboardSubmissions(self.config).row(request)['reason'])
        self.assertEqual([row for row in reservations(self.root) if row['job'] == 1], [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT outcome FROM applier_messages WHERE id=?', (f'dashboard-submit-{request}',)).fetchone(), ('cancelled',))
        [record] = self.store.requeues(1)
        self.assertEqual([(r['event_id'], r['used_at']) for r in record['retired']['applier_submit_auth']], [(request, None)])
        self.assertEqual(self.requeue(1)[0]['status'], 'already_queued')

        self.queue.transition(1, ApplicationState.RESUME_READY, application_folder_relpath='existing-new-1')
        new, started = self.started_with_tab('new', 'existing-new-1')
        self.assertEqual(self.store.request_submit(1, started), (False, 'not_authorized'), 'the withdrawn approval does not carry over')
        again = self.confirm(new, started)
        self.assertEqual(self.requests(1), {request: 'not_submitted', again: 'queued'})
        self.assertEqual(self.store.request_submit(1, started), (True, 'authorized'))

    def test_the_proc_reader_gives_a_running_process_start_and_none_for_a_missing_one(self):
        self.assertEqual(process_start(os.getpid()), Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
        self.assertIsNone(process_start(2 ** 22 + 1))

    def test_the_boot_reader_gives_a_time_after_2020_and_before_now(self):
        self.assertTrue(1577836800 < boot_time() < time.time())


if __name__ == '__main__':
    unittest.main()
