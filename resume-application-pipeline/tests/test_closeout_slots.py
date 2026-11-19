import unittest

import test_capacity as fixture


def pending_closeout(job_id):
    return {'job_id': job_id, 'counted': True, 'submitted': True, 'synced': True, 'closed': False,
            'turn_state': 'ended', 'terminal': False, 'user_submitted': False, 'paused': False,
            'blocked': None, 'owner': f'owner-{job_id}', 'reserved': False, 'company_key': f'c{job_id}'}


class CloseoutSlotTest(unittest.TestCase):
    worker = fixture.CapacityTest.worker

    def run_with(self, closeouts):
        w = self.worker(set(), count=6)
        w.progress_monitor = True
        w._progress_snapshot = {'errors': [], 'dispatch_errors': [], 'target': 100, 'max_per_company': None,
                                'counts': {'submitted': closeouts, 'reserved': 0},
                                'jobs': [pending_closeout(99 - i) for i in range(closeouts)]}
        self.addCleanup(w.close)
        w._schedule_applications()
        return w._applications.submit.call_count

    # 2026-10-15 (later) user decision: a closeout whose owner turn has ended has
    # nobody closing it, so it no longer holds a slot; the blocker audit reports it.
    def test_ended_owner_closeout_holds_no_slot(self):
        self.assertEqual(self.run_with(1), 4)

    def test_many_ended_owner_closeouts_hold_no_slots(self):
        self.assertEqual(self.run_with(4), 4)

    def test_no_pending_closeout_uses_all_slots(self):
        self.assertEqual(self.run_with(0), 4)


if __name__ == '__main__':
    unittest.main()
