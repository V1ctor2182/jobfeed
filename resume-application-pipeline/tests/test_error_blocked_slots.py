import unittest

import test_capacity as fixture
from pipeline.lifecycle import ApplicationState as S
from pipeline.store import QueueItem


def job(job_id, company):
    return {'job_id': job_id, 'company_key': company, 'counted': True, 'terminal': False, 'user_submitted': False,
            'paused': False, 'blocked': None, 'owner': None, 'reserved': False, 'submitted': False,
            'synced': False, 'closed': False, 'turn_state': 'not_started'}


class ErrorBlockedSlotTest(unittest.TestCase):
    worker = fixture.CapacityTest.worker

    def run_with(self, errors):
        # Three live applications plus ElevenLabs 506205: Ready for Review, no live owner.
        w = self.worker({'owner-1', 'owner-2', 'owner-3'}, count=1)
        w.progress_monitor = True
        live = [QueueItem(job_id=i, state=S.APPLYING, codex_application_session_id=f'owner-{i}') for i in (1, 2, 3)]
        stuck = QueueItem(job_id=9, state=S.READY_FOR_REVIEW, codex_application_session_id='owner-9')
        w.pipeline.queue.list_active.return_value = live + [stuck] + w.pipeline.queue.list_active.return_value
        w._progress_snapshot = {'errors': errors, 'dispatch_errors': errors, 'target': 100, 'max_per_company': None,
                                'counts': {'submitted': 0, 'reserved': 0},
                                'jobs': [job(0, 'acme'), job(9, 'elevenlabs')] + [job(i, f'c{i}') for i in (1, 2, 3)]}
        self.addCleanup(w.close)
        w._schedule_applications()
        return 0 in w._application_futures

    def test_idle_error_blocked_job_frees_its_slot(self):
        # 2026-10-15 user decision: a job the monitor stops, with no agent running, holds no slot.
        self.assertTrue(self.run_with(['9:missing_submission_evidence']))

    def test_idle_retry_later_frees_its_slot(self):
        # 2026-10-15: with the monitor on, automatic retry is disabled, so an idle
        # Retry Later job (SCS 508529 waiting on reCAPTCHA) can never move by itself.
        w = self.worker({'owner-1', 'owner-2', 'owner-3'}, count=1)
        w.progress_monitor = True
        live = [QueueItem(job_id=i, state=S.APPLYING, codex_application_session_id=f'owner-{i}') for i in (1, 2, 3)]
        idle = QueueItem(job_id=9, state=S.RETRY_LATER, codex_application_session_id='owner-9', next_retry_at=None)
        w.pipeline.queue.list_active.return_value = live + [idle] + w.pipeline.queue.list_active.return_value
        w._progress_snapshot = {'errors': [], 'dispatch_errors': [], 'target': 100, 'max_per_company': None,
                                'counts': {'submitted': 0, 'reserved': 0},
                                'jobs': [job(0, 'acme'), job(9, 'scs')] + [job(i, f'c{i}') for i in (1, 2, 3)]}
        self.addCleanup(w.close)
        w._schedule_applications()
        self.assertIn(0, w._application_futures)


if __name__ == '__main__':
    unittest.main()
