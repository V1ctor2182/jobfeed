import unittest

import test_capacity as fixture


def job(job_id, company):
    return {'job_id': job_id, 'company_key': company, 'counted': True, 'terminal': False, 'user_submitted': False,
            'paused': False, 'blocked': None, 'owner': None, 'reserved': False, 'submitted': False,
            'synced': False, 'closed': False, 'turn_state': 'not_started'}


class JobScopedErrorTest(unittest.TestCase):
    worker = fixture.CapacityTest.worker

    def allows(self, errors, action, job_id=None):
        w = self.worker(set(), count=0)
        self.addCleanup(w.close)
        w.progress_monitor = True
        w._progress_snapshot = {'errors': errors, 'dispatch_errors': errors, 'target': 100, 'max_per_company': None,
                                'counts': {'submitted': 0, 'reserved': 0},
                                'jobs': [job(509284, 'etched'), job(509279, 'etched'), job(5, 'acme')]}
        return w._progress_allows(action, job_id)

    def test_one_job_error_does_not_stop_other_companies(self):
        self.assertTrue(self.allows(['509284:missing_submission_evidence'], 'application_started', 5))
        self.assertTrue(self.allows(['509284:missing_submission_evidence'], 'dispatch'))

    def test_job_error_blocks_that_job_and_same_company(self):
        self.assertFalse(self.allows(['509284:owner_conflict'], 'application_started', 509284))
        self.assertFalse(self.allows(['509284:owner_conflict'], 'application_started', 509279))

    def test_missing_receipt_blocks_only_that_job(self):
        # 2026-10-15 user decision: the company's other postings keep running.
        self.assertFalse(self.allows(['509284:missing_submission_evidence'], 'application_started', 509284))
        self.assertTrue(self.allows(['509284:missing_submission_evidence'], 'application_started', 509279))

    def test_user_submitted_job_can_still_be_reconciled(self):
        # 2026-10-15: user-finished applications stayed "Needs Input" forever because the
        # read-only Jobfeed reconcile was blocked for them.
        w = self.worker(set(), count=0)
        self.addCleanup(w.close)
        w.progress_monitor = True
        done = {**job(7, 'siemens'), 'user_submitted': True}
        w._progress_snapshot = {'errors': [], 'dispatch_errors': [], 'target': 100, 'max_per_company': None,
                                'counts': {'submitted': 0, 'reserved': 0}, 'jobs': [done]}
        self.assertTrue(w._progress_allows('submission_reconciled', 7))
        self.assertFalse(w._progress_allows('application_started', 7))

    def test_global_error_still_stops_everything(self):
        self.assertFalse(self.allows(['jobfeed_readback:OSError'], 'application_started', 5))
        self.assertFalse(self.allows(['session_index:ValueError'], 'dispatch'))


if __name__ == '__main__':
    unittest.main()
