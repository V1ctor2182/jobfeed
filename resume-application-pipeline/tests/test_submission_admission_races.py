from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from pipeline.dashboard_submission import finish_worker
from pipeline.decision_queue import DecisionQueue
from pipeline.worker import OrchestratorWorker
from tests import test_dashboard_submission as fixture


class SubmissionAdmissionRaceTests(unittest.TestCase):
    setUp = fixture.DashboardSubmissionTests.setUp
    preview = fixture.DashboardSubmissionTests.preview
    confirm = fixture.DashboardSubmissionTests.confirm

    def test_worker_exit_respects_capacity_across_two_confirmed_jobs(self):
        store = self.bank.read()
        self.bank.mutate({'action': 'approve', 'entry_id': store['entries'][0]['id'],
            'expected_version': 1, 'expected_revision': store['revision']}, dashboard=True)
        manifest = dict(self.fixture.manifest)
        manifest['ordered_candidates'] = [*manifest['ordered_candidates'],
            {'id': 2, 'company': 'Second', 'title': 'Engineer'}]
        self.fixture.scope.write_text(json.dumps(manifest))
        second_folder = self.config.applications_root / 'second'
        second_folder.mkdir()
        with sqlite3.connect(self.fixture.db) as db:
            db.execute("INSERT INTO jobs VALUES(2,'Second','Engineer','Synthetic JD','https://example.test/second',NULL)")
            db.execute("INSERT INTO application_queue(job_id,state,application_folder_relpath,codex_application_session_id) VALUES(2,'Needs Input','second','sibling')")
        ledger = DecisionQueue(self.fixture.root / '.pipeline/blockers.sqlite3',
            scope=str(self.fixture.scope), controller='controller')
        ledger.reconcile_membership([1, 2], '2026-10-23T12:00:00+00:00')
        ledger.db.close()
        sibling_path = self.fixture.root / 'sibling-rollout.jsonl'
        sibling_path.write_text('\n'.join(json.dumps(event) for event in [
            {'type': 'session_meta', 'payload': {'id': 'sibling', 'cwd': str(self.fixture.root)}},
            {'type': 'event_msg', 'payload': {'turn_id': 'old-turn', 'type': 'task_complete'}},
        ]) + '\n')
        sibling = {**self.fixture.thread, 'id': 'sibling', 'path': str(sibling_path)}
        second_request = str(uuid4())
        worker = SimpleNamespace(pipeline=SimpleNamespace(config=self.config, manifest_path=self.fixture.scope))
        with patch('pipeline.dashboard_submission.read_thread', side_effect=lambda owner:
                self.fixture.read(owner) if owner == 'owner' else sibling), \
                patch('pipeline.dashboard_submission.selected_materials', side_effect=lambda folder, **identity:
                      {**identity, 'resume_id': 'original-pdf'}), \
                (self.fixture.root / '.pipeline/worker-progress.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.confirm()['state'], 'preparing')
            preview = self.service.preview(batch_id='test', job_id=2)
            self.assertTrue(preview['eligible'])
            self.assertEqual(self.service.confirm(request_id=second_request,
                expected_preview=preview, confirmed=True)['state'], 'preparing')
            finish_worker(worker, handle)
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertEqual(sorted(self.service.row(request)['state'] for request in
            [self.request, second_request]), ['preparing', 'queued'])

    def test_confirmation_after_drain_decision_is_delivered_during_real_worker_exit(self):
        worker = OrchestratorWorker(
            SimpleNamespace(config=self.config, manifest_path=self.fixture.scope),
            emit=lambda event: None,
        )
        self.addCleanup(worker.close)
        closing = Event()
        release = Event()
        close = worker.close

        def pause_before_unlock():
            close()
            closing.set()
            if not release.wait(3):
                raise AssertionError('Test did not release worker exit')

        def finish_tick():
            worker._progress_done = True

        with patch.object(worker, '_user_stopped', return_value=False), \
                patch.object(worker, 'tick', side_effect=finish_tick), \
                patch.object(worker, 'close', side_effect=pause_before_unlock), \
                ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(worker.run_forever)
            try:
                self.assertTrue(closing.wait(2))
                accepted = self.confirm()
                self.assertEqual(accepted['state'], 'preparing')
                self.assertEqual(self.service.owner_status(request_id=self.request)['state'], 'preparing')
                self.assertEqual(self.fixture.sent, [])
            finally:
                release.set()
            running.result(3)
        self.assertTrue(worker._worker_lock_handle.closed)
        self.assertEqual(self.service.row(self.request)['state'], 'queued')
        self.assertEqual(len(self.fixture.sent), 1)
        self.fixture.running()
        self.assertEqual(self.service.owner_status(request_id=self.request)['state'], 'running')
        self.assertTrue(self.service.permits('owner', 'new-turn', scope=str(self.fixture.scope), job=1))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_status_cannot_report_unsent_while_admitted_send_is_in_progress(self):
        with (self.fixture.root / '.pipeline/worker-progress.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.confirm()['state'], 'preparing')
        entered = Event()
        release = Event()
        status_started = Event()
        status_entered = Event()
        original_thread = self.service._thread
        original_status = self.service._status
        clock = [datetime.now(timezone.utc)]
        first = [True]

        def delayed_thread(preview, **kwargs):
            if first[0]:
                first[0] = False
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Test did not release owner inspection')
            return original_thread(preview, **kwargs)

        def poll():
            status_started.set()
            return self.service.status(request_id=self.request)

        def inspect_status(**kwargs):
            status_entered.set()
            return original_status(**kwargs)

        with patch.object(self.service, '_thread', side_effect=delayed_thread), \
                patch.object(self.service, '_status', side_effect=inspect_status), \
                patch('pipeline.dashboard_submission.now', side_effect=lambda: clock[0]), \
                ThreadPoolExecutor(max_workers=2) as pool:
            dispatch = pool.submit(self.service._advance_stopped, self.request)
            try:
                self.assertTrue(entered.wait(2))
                clock[0] += timedelta(days=30)
                status = pool.submit(poll)
                self.assertTrue(status_started.wait(2))
                self.assertFalse(status_entered.wait(0.1))
            finally:
                release.set()
            self.assertEqual(dispatch.result(3)['state'], 'queued')
            observed = status.result(3)
        self.assertEqual(observed['state'], 'queued')
        self.assertFalse(observed['delivery_not_started'])
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertIsNotNone(self.service.row(self.request)['prompt'])
