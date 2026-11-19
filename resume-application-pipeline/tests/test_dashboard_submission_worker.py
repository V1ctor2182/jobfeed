import fcntl
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pipeline.dashboard_submission import consume_worker, finish_worker
from pipeline.decision_queue import DecisionQueue
from pipeline.submission_reservation import assert_available, occupies_slot, reservations
from pipeline.worker import OrchestratorWorker, WorkerSettings
from tests import test_dashboard_submission as baseline


class DashboardWorkerSubmissionTests(unittest.TestCase):
    setUp = baseline.DashboardSubmissionTests.setUp
    preview = baseline.DashboardSubmissionTests.preview
    confirm = baseline.DashboardSubmissionTests.confirm

    def worker(self):
        self.fixture.scope.with_name('test-handoff.json').write_text(json.dumps({
            'manifest': str(self.fixture.scope), 'controller': 'controller', 'status': 'launch_admitted'}))
        with self.fixture.gate._db() as db:
            db.execute('DELETE FROM execution_cancellations')
        ledger = DecisionQueue(self.fixture.root / '.pipeline/blockers.sqlite3', scope=str(self.fixture.scope), controller='controller')
        ledger.db.execute('UPDATE dq_scopes SET active=1')
        self.addCleanup(ledger.db.close)
        pipeline = SimpleNamespace(config=self.config, queue=self.fixture.queue, batch_limit=2,
            manifest_path=self.fixture.scope, applications=SimpleNamespace(execution_scope=str(self.fixture.scope)),
            batch_ids=(1,), next_deferred_application=lambda: None)
        monitor = SimpleNamespace(ledger=ledger, sources=SimpleNamespace(manifest=self.fixture.scope))
        worker = OrchestratorWorker(pipeline, WorkerSettings(application_workers=2), progress_monitor=monitor, emit=lambda event: None)
        worker._active_application_threads = lambda: set()
        handle = (self.fixture.root / '.pipeline/worker-progress.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker._worker_lock_handle = handle
        self.addCleanup(handle.close)
        self.addCleanup(worker.close)
        return worker

    def test_active_worker_accepts_once_consumes_and_releases_only_exact_turn(self):
        worker = self.worker()
        preview = self.preview()
        self.assertEqual(self.confirm(preview)['state'], 'preparing')
        self.assertEqual(self.confirm(preview)['state'], 'preparing')
        self.assertIsNone(self.fixture.queue.submit_authorization(1))
        self.assertEqual(self.bank.read()['entries'][0]['versions'][-1]['state'], 'pending_review')
        self.assertEqual(consume_worker(worker)[0]['state'], 'queued')
        self.assertFalse(worker._worker_lock_handle.closed)
        self.fixture.running()
        self.assertEqual(consume_worker(worker)[0]['state'], 'running')
        self.assertTrue(self.service.permits('owner', 'new-turn', scope=str(self.fixture.scope), job=1))
        self.assertFalse(self.service.permits('owner', 'old-turn'))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_busy_owner_waits_without_early_approval_then_dispatches(self):
        worker = self.worker()
        self.fixture.thread['turns'][-1]['status'] = 'inProgress'
        self.fixture.thread['status'] = {'type': 'active'}
        self.confirm()
        self.assertEqual(consume_worker(worker)[0]['state'], 'preparing')
        self.assertIsNone(self.fixture.queue.submit_authorization(1))
        self.assertEqual(self.fixture.sent, [])
        self.fixture.thread['turns'][-1]['status'] = 'completed'
        self.fixture.thread['status'] = {'type': 'idle'}
        self.assertEqual(consume_worker(worker)[0]['state'], 'queued')

    def test_capacity_wait_survives_days_and_dispatches_when_slot_available(self):
        worker = self.worker()
        worker._active_application_threads = lambda: {'other-1', 'other-2'}
        self.confirm()
        self.assertEqual(consume_worker(worker), [])
        with self.service.db() as db:
            db.execute('UPDATE requests SET expires_at=?', ('2000-01-01T00:00:00+00:00',))
        with patch('pipeline.dashboard_submission.now', return_value=datetime.now(timezone.utc)+timedelta(days=30)):
            self.assertEqual(consume_worker(worker), [])
            self.assertEqual(self.service.row(self.request)['state'], 'preparing')
            self.assertEqual(self.fixture.sent, [])
            self.assertIsNone(self.fixture.queue.submit_authorization(1))
            worker._active_application_threads = lambda: set()
            self.assertEqual(consume_worker(worker)[0]['state'], 'queued')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_unknown_send_is_never_repeated_by_consumer_or_fresh_confirmation(self):
        worker = self.worker()
        self.confirm()
        with patch('pipeline.dashboard_submission.send_existing', side_effect=TimeoutError) as sender:
            self.assertEqual(consume_worker(worker)[0]['state'], 'unknown')
            consume_worker(worker)
            consume_worker(worker)
            sender.assert_called_once()
        self.request = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'earlier submission'):
            self.confirm()

    def test_changed_stop_blocks_unsent_and_revokes_dispatched_turn(self):
        worker = self.worker()
        self.confirm()
        self.fixture.gate.cancel_scope(str(self.fixture.scope), 'new-stop')
        self.assertTrue(consume_worker(worker)[0]['delivery_not_started'])
        self.assertEqual(self.fixture.sent, [])

    def test_terminal_turn_releases_slot_but_fences_job_until_late_receipt(self):
        worker = self.worker()
        self.confirm()
        consume_worker(worker)
        self.fixture.running()
        self.fixture.thread['turns'][-1]['status'] = 'completed'
        self.fixture.thread['status'] = {'type': 'idle'}
        self.assertEqual(consume_worker(worker)[0]['state'], 'turn_ended')
        row = reservations(self.fixture.root)[0]
        self.assertFalse(occupies_slot(row))
        with self.assertRaisesRegex(ValueError, 'reserved'):
            assert_available(self.fixture.root, 1)
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('INSERT INTO applied(job_id) VALUES(1)')
        self.assertEqual(consume_worker(worker)[0]['state'], 'submitted')
        self.assertEqual(reservations(self.fixture.root), [])
        self.assertEqual(self.service.row(self.request)['state'], 'submitted')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_sync_pending_still_fences_job_and_worker_retries_only_sync(self):
        worker = self.worker()
        self.confirm()
        consume_worker(worker)
        (self.fixture.folder / 'submission-receipt.json').write_text('{}')
        evidence = SimpleNamespace(artifact_path=self.fixture.folder / 'confirmation.png')
        with patch('jobfeed.submission_evidence.read_confirmation', return_value=evidence), patch(
                'pipeline.orchestrator.ApplicationPipeline.finish_submission', side_effect=OSError('sync unavailable')):
            self.assertEqual(consume_worker(worker)[0]['state'], 'sync_pending')
        self.assertFalse(occupies_slot(reservations(self.fixture.root)[0]))
        with self.assertRaisesRegex(ValueError, 'reserved'):
            assert_available(self.fixture.root, 1)
        def sync(job):
            with sqlite3.connect(self.fixture.db) as db:
                db.execute('INSERT INTO applied(job_id) VALUES(?)', (job,))
        with patch('jobfeed.submission_evidence.read_confirmation', return_value=evidence), patch(
                'pipeline.orchestrator.ApplicationPipeline.finish_submission', side_effect=sync):
            self.assertEqual(consume_worker(worker)[0]['state'], 'submitted')
        self.assertEqual(reservations(self.fixture.root), [])
        self.assertEqual(len(self.fixture.sent), 1)

    def test_exit_handoff_dispatches_confirmation_accepted_after_drain_decision(self):
        worker = self.worker()
        worker._progress_done = True
        self.assertEqual(self.confirm()['state'], 'preparing')
        result = finish_worker(worker, worker._worker_lock_handle)
        self.assertEqual(result[0]['state'], 'queued')
        self.assertTrue(worker._worker_lock_handle.closed)
        self.assertEqual(len(self.fixture.sent), 1)

    def test_owner_status_reconciles_dispatched_exact_turn_without_dashboard_poll(self):
        worker = self.worker()
        self.confirm()
        self.assertEqual(self.service.owner_status(request_id=self.request)['state'], 'preparing')
        self.assertEqual(self.fixture.sent, [])
        finish_worker(worker, worker._worker_lock_handle)
        self.fixture.running()
        self.assertEqual(self.service.owner_status(request_id=self.request)['state'], 'running')
        self.assertTrue(self.service.permits('owner', 'new-turn', job=1))
        self.assertEqual(len(self.fixture.sent), 1)
        self.fixture.gate.cancel_scope(str(self.fixture.scope), 'new-stop')
        self.assertEqual(self.service.owner_status(request_id=self.request)['state'], 'blocked')
        self.assertFalse(self.service.permits('owner', 'new-turn', job=1))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_unsent_pin_failure_restores_exact_prior_authorization(self):
        from pipeline.dashboard_submission import DashboardSubmissions
        worker = self.worker()
        self.confirm()
        original = DashboardSubmissions._preview
        def drift(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if result['submit_authorization']:
                result['selected_resume'] = {'resume_id': 'changed'}
            return result
        with patch.object(DashboardSubmissions, '_preview', drift):
            result = consume_worker(worker)[0]
        self.assertTrue(result['delivery_not_started'])
        self.assertIsNone(self.fixture.queue.submit_authorization(1))
        self.assertEqual(self.bank.read()['entries'][0]['versions'][-1]['state'], 'approved')
        self.assertEqual(self.fixture.sent, [])

    def test_failed_pin_cleanup_preserves_newer_authorization(self):
        from pipeline.dashboard_submission import DashboardSubmissions
        worker = self.worker()
        self.confirm()
        original = DashboardSubmissions._preview
        def revoke(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if result['submit_authorization']:
                self.fixture.queue.record_submit_authorization(1, {'authorized': False, 'user_text': 'Stop',
                    'source_thread_id': 'controller', 'source_message_id': 'newer-revocation'})
                result = original(service, *args, **kwargs)
            return result
        with patch.object(DashboardSubmissions, '_preview', revoke):
            result = consume_worker(worker)[0]
        self.assertTrue(result['delivery_not_started'])
        self.assertFalse(self.fixture.queue.submit_authorization(1)['authorized'])
        self.assertEqual(self.fixture.queue.submit_authorization(1)['source_message_id'], 'newer-revocation')
        self.assertEqual(self.fixture.sent, [])

    def test_failed_authorization_restore_keeps_unsent_request_reserved(self):
        from pipeline.dashboard_submission import DashboardSubmissions
        worker = self.worker()
        self.confirm()
        original = DashboardSubmissions._preview
        def drift(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if result['submit_authorization']:
                raise ValueError('pin drift')
            return result
        with patch.object(DashboardSubmissions, '_preview', drift), patch.object(
                DashboardSubmissions, '_restore_unsent_authorization', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                consume_worker(worker)
        self.assertEqual(self.service.row(self.request)['state'], 'preparing')
        self.assertFalse(self.service._public(self.service.row(self.request))['delivery_not_started'])
        with self.assertRaisesRegex(ValueError, 'reserved'):
            assert_available(self.fixture.root, 1)
        self.assertEqual(self.fixture.sent, [])
