import sqlite3
import unittest

import test_capacity as fixture
from pipeline.lifecycle import ApplicationState as S
from pipeline.store import QueueItem


def waiting(n, state=S.NEEDS_INPUT):
    return [QueueItem(job_id=10 + i, state=state, codex_application_session_id=f'owner-{i}') for i in range(n)]


class WaitingSlotTest(unittest.TestCase):
    worker = fixture.CapacityTest.worker

    def test_idle_user_waits_do_not_block_new_applications(self):
        w = self.worker(set(), count=1)
        verification = QueueItem(job_id=20, state=S.NEEDS_VERIFICATION, codex_application_session_id='owner-v')
        w.pipeline.queue.list_active.return_value = waiting(4) + [verification] + w.pipeline.queue.list_active.return_value
        try:
            w._schedule_applications()
            self.assertIn(0, w._application_futures)
        finally:
            w.close()

    def test_waiting_owner_that_is_running_still_counts(self):
        w = self.worker({'owner-0', 'owner-1', 'owner-2', 'owner-3'}, count=1)
        w.pipeline.queue.list_active.return_value = waiting(4) + w.pipeline.queue.list_active.return_value
        try:
            w._schedule_applications()
            self.assertNotIn(0, w._application_futures)
        finally:
            w.close()


class SubmissionReservationSlotTest(unittest.TestCase):
    """Four Appliers whose Submit outcome waits on the user, and one job ready to start, with one slot left."""
    worker = fixture.CapacityTest.worker

    def run_with(self, owners, *, state='blocked', waiting_on_user=frozenset()):
        w = self.worker(set(owners), count=1)
        w._waiting_application_threads = lambda active: set(waiting_on_user)
        w.pipeline.queue.list_active.return_value = [
            QueueItem(job_id=10 + i, state=S.NEEDS_VERIFICATION, codex_application_session_id=owner) for i, owner in enumerate(owners)
        ] + w.pipeline.queue.list_active.return_value
        path = w.pipeline.config.resume_root / '.pipeline/dashboard-submissions.sqlite3'
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE requests(id TEXT, job INTEGER, owner TEXT, state TEXT, prompt TEXT, preview TEXT, terminal_turn TEXT)')
            db.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,NULL)',
                           [(f'r{i}', 10 + i, owner, state, 'Submit now', '{}') for i, owner in enumerate(owners)])
        try:
            w._schedule_applications()
            return 0 in w._application_futures
        finally:
            w.close()

    def test_blocked_claude_submissions_whose_appliers_wait_on_the_user_hold_no_slot(self):
        owners = [f'{i:032x}' for i in range(1, 5)]
        self.assertTrue(self.run_with(owners, waiting_on_user=owners))

    def test_a_queued_claude_submission_keeps_its_slot_for_the_click_it_is_about_to_send(self):
        owners = [f'{i:032x}' for i in range(1, 5)]
        self.assertFalse(self.run_with(owners, state='queued', waiting_on_user=owners))

    def test_a_blocked_codex_submission_still_holds_its_slot(self):
        self.assertFalse(self.run_with([f'codex-{i}' for i in range(4)]))


if __name__ == '__main__':
    unittest.main()


class ProducerWaitTest(unittest.TestCase):
    """2026-11-16: 100 Applying jobs whose Appliers had parked or ended kept the producer, and so every admission, off for two days."""
    worker = fixture.CapacityTest.worker

    def producer_runs(self, busy, *, held=False):
        import dataclasses
        from unittest.mock import Mock
        from pipeline.store import Hold
        owners = [f'{i:032x}' for i in range(1, 4)]
        w = self.worker(set(busy), count=0)
        w.settings = dataclasses.replace(w.settings, queue_target=3)
        hold = Hold('h', 'captcha', 'x', None, '2026-11-09T00:00:00+00:00') if held else None
        w.pipeline.queue.list_active.return_value = [
            QueueItem(job_id=10 + i, state=S.APPLYING, codex_application_session_id=owner, hold=hold) for i, owner in enumerate(owners)]
        w._producer.shutdown()
        w._producer = Mock()
        try:
            w._schedule_applications()
            w._schedule_producer()
            return w._producer.submit.called
        finally:
            w.close()

    def test_claude_jobs_whose_appliers_stopped_working_do_not_fill_the_queue_target(self):
        self.assertTrue(self.producer_runs(set()))

    def test_claude_appliers_still_working_fill_the_queue_target(self):
        self.assertFalse(self.producer_runs({f'{i:032x}' for i in range(1, 4)}))

    def test_held_jobs_do_not_fill_the_queue_target(self):
        self.assertTrue(self.producer_runs({f'{i:032x}' for i in range(1, 4)}, held=True))

    def test_before_any_count_of_working_appliers_every_started_job_counts(self):
        w = self.worker(set(), count=0)
        from unittest.mock import Mock
        import dataclasses
        w.settings = dataclasses.replace(w.settings, queue_target=1)
        w.pipeline.queue.list_active.return_value = [QueueItem(job_id=10, state=S.APPLYING, codex_application_session_id=f'{1:032x}')]
        w._producer.shutdown()
        w._producer = Mock()
        try:
            w._schedule_producer()
            self.assertFalse(w._producer.submit.called)
        finally:
            w.close()
