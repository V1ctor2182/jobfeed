import sqlite3
import unittest
from unittest.mock import patch

from pipeline.dashboard_submission import DashboardSubmissions
from tests import test_submission_admission_races as baseline


class SubmissionReconcilerTests(unittest.TestCase):
    setUp = baseline.SubmissionAdmissionRaceTests.setUp
    preview = baseline.SubmissionAdmissionRaceTests.preview
    confirm = baseline.SubmissionAdmissionRaceTests.confirm

    def accepted_pair(self):
        baseline.SubmissionAdmissionRaceTests.test_worker_exit_respects_capacity_across_two_confirmed_jobs(self)
        sibling = {**self.fixture.thread, 'id': 'sibling',
                   'path': str(self.fixture.root / 'sibling-rollout.jsonl')}
        for name, replacement in [
            ('read_thread', lambda owner: self.fixture.read(owner) if owner == 'owner' else sibling),
            ('selected_materials', lambda folder, **identity: {**identity, 'resume_id': 'original-pdf'}),
        ]:
            replacement_patch = patch('pipeline.dashboard_submission.' + name, side_effect=replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)
        with self.service.db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM requests ORDER BY created_at')]
        return rows

    def test_confirmed_pair_drains_after_worker_exit_without_status_polling(self):
        first, second = self.accepted_pair()
        self.assertEqual([first['state'], second['state']], ['queued', 'preparing'])
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('INSERT INTO applied(job_id) VALUES(?)', (first['job'],))
        restarted = DashboardSubmissions(self.config)
        result = restarted.reconcile_pending()
        self.assertEqual(self.service.row(first['id'])['state'], 'submitted')
        self.assertEqual(self.service.row(second['id'])['state'], 'queued')
        self.assertEqual(len(self.fixture.sent), 2)
        self.assertEqual(result['checked'], 2)

    def test_background_binds_actual_turn_and_syncs_late_receipt(self):
        from types import SimpleNamespace
        self.confirm()
        self.fixture.running()
        self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'running')
        self.assertTrue(self.service.permits('owner', 'new-turn', job=1))
        self.fixture.thread['turns'][-1]['status'] = 'completed'
        self.fixture.thread['status'] = {'type': 'idle'}
        self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'turn_ended')
        (self.fixture.folder / 'submission-receipt.json').write_text('{}')
        evidence = SimpleNamespace(artifact_path=self.fixture.folder / 'confirmation.png')
        with patch('jobfeed.submission_evidence.read_confirmation', return_value=evidence), patch(
                'pipeline.orchestrator.ApplicationPipeline.finish_submission', side_effect=OSError('offline')):
            self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'sync_pending')
        def sync(job):
            with sqlite3.connect(self.fixture.db) as db:
                db.execute('INSERT INTO applied(job_id) VALUES(?)', (job,))
        with patch('jobfeed.submission_evidence.read_confirmation', return_value=evidence), patch(
                'pipeline.orchestrator.ApplicationPipeline.finish_submission', side_effect=sync):
            DashboardSubmissions(self.config).reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'submitted')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_network_outage_before_delivery_defers_then_recovers_once(self):
        from pipeline.codex_app_server import AppServerTimeout
        with patch('pipeline.dashboard_submission.read_thread', side_effect=AppServerTimeout('offline')):
            result = self.confirm()
            expires = self.service.row(self.request)['expires_at']
            self.service.reconcile_pending()
        self.assertEqual(result['state'], 'preparing')
        self.assertEqual(self.service.row(self.request)['expires_at'], expires)
        self.assertIsNone(self.fixture.queue.submit_authorization(1))
        self.assertEqual(self.fixture.sent, [])
        DashboardSubmissions(self.config).reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'queued')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_lost_send_receipt_and_read_outage_only_reconcile_never_resend(self):
        from pipeline.codex_app_server import AppServerTimeout
        def lost_receipt(root, owner, prompt):
            self.fixture.send(root, owner, prompt)
            raise TimeoutError('outcome unknown')
        with patch('pipeline.dashboard_submission.send_existing', side_effect=lost_receipt):
            self.assertEqual(self.confirm()['state'], 'unknown')
        self.fixture.running()
        with patch('pipeline.dashboard_submission.read_thread', side_effect=AppServerTimeout('offline')):
            self.service.reconcile_pending()
            self.assertEqual(self.service.row(self.request)['state'], 'unknown')
        DashboardSubmissions(self.config).reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'running')
        self.assertIsNone(self.service.row(self.request)['reason'])
        self.assertEqual(len(self.fixture.sent), 1)

    def test_old_preparing_confirmation_survives_days_and_backend_restart(self):
        from datetime import datetime, timedelta, timezone
        from tests.test_dashboard_submission_worker import DashboardWorkerSubmissionTests
        worker = DashboardWorkerSubmissionTests.worker(self)
        self.confirm()
        with self.service.db() as db:
            db.execute('UPDATE requests SET expires_at=? WHERE id=?',
                       ('2000-01-01T00:00:00+00:00', self.request))
        restarted = DashboardSubmissions(self.config)
        with patch('pipeline.dashboard_submission.now', return_value=datetime.now(timezone.utc)+timedelta(days=30)):
            self.assertEqual(restarted.reconcile_pending()['results'][0]['state'], 'preparing')
            self.assertEqual(self.fixture.sent, [])
            worker._worker_lock_handle.close()
            self.assertEqual(restarted.reconcile_pending()['results'][0]['state'], 'queued')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_aged_confirmation_still_rejects_changed_application_details(self):
        from datetime import datetime, timedelta, timezone
        from tests.test_dashboard_submission_worker import DashboardWorkerSubmissionTests
        worker = DashboardWorkerSubmissionTests.worker(self)
        self.confirm()
        store = self.bank.read()
        self.bank.mutate({'action': 'edit', 'entry_id': store['entries'][0]['id'],
            'expected_version': 1, 'expected_revision': store['revision'],
            'draft': {'question': 'Why us?', 'answer': 'Changed answer.',
                      'scope': {'kind': 'job', 'company': 'Rilla', 'job_id': '1'}}})
        worker._worker_lock_handle.close()
        with patch('pipeline.dashboard_submission.now', return_value=datetime.now(timezone.utc)+timedelta(days=30)):
            result = DashboardSubmissions(self.config).reconcile_pending()['results'][0]
        self.assertEqual(result['state'], 'blocked')
        self.assertTrue(result['delivery_not_started'])
        self.assertEqual(self.fixture.sent, [])

    def test_revocation_after_acceptance_prevents_unsent_dispatch(self):
        from tests.test_dashboard_submission_worker import DashboardWorkerSubmissionTests
        worker = DashboardWorkerSubmissionTests.worker(self)
        self.confirm()
        self.fixture.gate.cancel_scope(str(self.fixture.scope), 'new-stop')
        worker._worker_lock_handle.close()
        result = self.service.reconcile_pending()['results'][0]
        self.assertTrue(result['delivery_not_started'])
        self.assertEqual(self.fixture.sent, [])

    def test_active_worker_keeps_preparing_ownership_and_dispatched_reconciliation(self):
        from pipeline.dashboard_submission import consume_worker
        from tests.test_dashboard_submission_worker import DashboardWorkerSubmissionTests
        worker = DashboardWorkerSubmissionTests.worker(self)
        self.confirm()
        self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'preparing')
        self.assertEqual(self.fixture.sent, [])
        consume_worker(worker)
        self.fixture.running()
        self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'running')
        self.assertTrue(self.service.permits('owner', 'new-turn', job=1))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_failed_first_row_does_not_starve_later_requests_after_restart(self):
        first, second = self.accepted_pair()
        with patch('pipeline.dashboard_submission.read_thread', side_effect=RuntimeError('unavailable')):
            first_pass = self.service.reconcile_pending(limit=1)
            second_pass = DashboardSubmissions(self.config).reconcile_pending(limit=1)
        self.assertEqual(first_pass['results'][0]['request_id'], first['id'])
        self.assertEqual(second_pass['results'][0]['request_id'], second['id'])
        self.assertTrue(first_pass['deferred'])
        self.assertEqual(len(self.fixture.sent), 1)

    def test_duplicate_reconcilers_and_admission_contention_defer_without_mutation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from pipeline.submission_reservation import admission_lock
        first, second = self.accepted_pair()
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('INSERT INTO applied(job_id) VALUES(?)', (first['job'],))
        entered, release = Event(), Event()
        def delayed_send(root, owner, prompt):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test did not release send')
            return self.fixture.send(root, owner, prompt)
        with patch('pipeline.dashboard_submission.send_existing', side_effect=delayed_send), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.service.reconcile_pending)
            try:
                self.assertTrue(entered.wait(2))
                other = DashboardSubmissions(self.config).reconcile_pending()
                self.assertEqual(other, {'checked': 0, 'results': [], 'deferred': True})
            finally:
                release.set()
            pending.result(3)
        self.assertEqual(len(self.fixture.sent), 2)
        with admission_lock(self.fixture.root), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.service.reconcile_pending).result(2)
        self.assertEqual(result, {'checked': 0, 'results': [], 'deferred': True})
        self.assertEqual(self.service.row(second['id'])['state'], 'queued')

    def test_cold_cli_and_nonempty_payload_never_create_a_ledger(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from pipeline.dashboard_submission import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for body, expected in [('{}', 0), ('{"request_id":"unconfirmed"}', 1)]:
                output = io.StringIO()
                with patch('sys.argv', ['submission', '--root', str(root), 'reconcile-pending']), patch('sys.stdin', io.StringIO(body)), contextlib.redirect_stdout(output):
                    self.assertEqual(main(), expected)
                self.assertEqual(list(root.iterdir()), [])

    def test_running_read_outage_retains_state_but_never_grants_unverified_permission(self):
        from pipeline.codex_app_server import AppServerTimeout
        from pipeline.dashboard_submission import permits
        self.confirm()
        self.fixture.running()
        self.service.reconcile_pending()
        with patch('pipeline.dashboard_submission.read_thread', side_effect=AppServerTimeout('offline')):
            self.service.reconcile_pending()
            self.assertEqual(self.service.row(self.request)['state'], 'running')
            self.assertFalse(permits(self.config, 'owner', 'new-turn', job=1))
        self.service.reconcile_pending()
        self.assertTrue(permits(self.config, 'owner', 'new-turn', job=1))
        self.assertEqual(len(self.fixture.sent), 1)

    def test_semantic_owner_change_blocks_instead_of_retrying_as_network_failure(self):
        from tests.test_dashboard_submission_worker import DashboardWorkerSubmissionTests
        worker = DashboardWorkerSubmissionTests.worker(self)
        self.confirm()
        worker._worker_lock_handle.close()
        with patch('pipeline.dashboard_submission.read_thread', return_value={**self.fixture.thread, 'id': 'foreign'}):
            self.service.reconcile_pending()
        self.assertEqual(self.service.row(self.request)['state'], 'blocked')
        self.service.reconcile_pending()
        self.assertEqual(self.fixture.sent, [])

    def test_pass_budget_stops_between_rows_and_later_pass_resumes_fairly(self):
        first, second = self.accepted_pair()
        with patch('pipeline.dashboard_submission.time.monotonic', side_effect=[0, 0, 16]):
            result = self.service.reconcile_pending()
        self.assertEqual(result['checked'], 1)
        self.assertTrue(result['deferred'])
        self.assertEqual(result['results'][0]['request_id'], first['id'])
        result = DashboardSubmissions(self.config).reconcile_pending(limit=1)
        self.assertEqual(result['results'][0]['request_id'], second['id'])
        self.assertEqual(len(self.fixture.sent), 1)
