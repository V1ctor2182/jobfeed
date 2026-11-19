import tempfile
import unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import Mock
from pipeline.lifecycle import ApplicationState
from pipeline.worker import OrchestratorWorker, WorkerSettings
from pipeline.store import QueueItem

class CapacityTest(unittest.TestCase):
    def test_pending_success_closeout_reserves_slot_not_whole_batch(self):
        # 2026-10-15 user decision: the closeout holds one slot (see test_closeout_slots).
        w = self.worker(set())
        w.progress_monitor = True
        w._progress_snapshot = {'errors': [], 'target': 10, 'counts': {'submitted': 1, 'reserved': 0}, 'jobs': [
            {'job_id': 99, 'submitted': True, 'synced': True,
             'closed': False, 'turn_state': 'ended'}]}
        try:
            self.assertTrue(w._progress_allows('application_started', 1))
            self.assertTrue(w._progress_allows('submission_cleanup', 1))
        finally:
            w.close()

    def worker(self, active, count=6):
        pipeline=Mock()
        # 2026-10-16: scheduling reads the Desktop outbox under resume_root for this scope.
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        pipeline.config.resume_root=Path(tmp.name)
        pipeline.manifest_path=str(Path(tmp.name)/'batch.json')
        pipeline.applications.execution_scope=pipeline.manifest_path
        pipeline.queue.paused.return_value=False
        pipeline.next_deferred_application.return_value=None
        pipeline.queue.list_active.return_value=[QueueItem(job_id=i,state=ApplicationState.RESUME_READY,codex_application_session_id=None) for i in range(count)]
        w=OrchestratorWorker(pipeline,WorkerSettings(),emit=lambda e:None)
        w._active_application_threads=lambda: active
        w._applications.shutdown()
        w._applications=Mock()
        w._applications.submit.side_effect=lambda *a:Future()
        return w

    def test_idle_review_waits_release_stop_before_submit_capacity(self):
        w=self.worker(set(),count=1)
        w.pipeline.stop_before_submit=True
        w.pipeline.submission_authorized.return_value=False
        waiting=[QueueItem(job_id=10+i,state=ApplicationState.READY_FOR_REVIEW,codex_application_session_id=f'luna-{i}') for i in range(6)]
        w.pipeline.queue.list_active.return_value=waiting+w.pipeline.queue.list_active.return_value
        try:
            w._schedule_applications()
            self.assertIn(0,w._application_futures)
        finally:w.close()

    def test_manual_task_reduces_dispatch_to_three(self):
        w=self.worker({'manual-hpe'})
        try:
            w._schedule_applications()
            self.assertEqual(w._applications.submit.call_count,3)
            w._schedule_applications()
            self.assertEqual(w._applications.submit.call_count,3)
        finally:w.close()

    def test_four_external_tasks_block_all_new_starts(self):
        w=self.worker({'a','b','c','d'})
        try:
            w._schedule_applications()
            self.assertEqual(w._applications.submit.call_count,0)
        finally:w.close()

    def test_unreadable_runtime_blocks_dispatch(self):
        w=self.worker(set())
        w._active_application_threads=Mock(side_effect=OSError('unavailable'))
        try:
            w._schedule_applications()
            self.assertEqual(w._applications.submit.call_count,0)
        finally:w.close()

