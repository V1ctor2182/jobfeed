import unittest

from batch_stub import batch_stub
from pipeline.lifecycle import ApplicationState as S
from pipeline.worker import OrchestratorWorker


def job(job_id, company):
    return {'job_id': job_id, 'company_key': company, 'counted': True, 'terminal': False, 'user_submitted': False,
            'paused': False, 'blocked': None, 'owner': None, 'reserved': False, 'submitted': False,
            'synced': False, 'closed': False, 'turn_state': 'not_started'}


class BlockedReadySlotTest(unittest.TestCase):
    def batch(self, blocked=()):
        return batch_stub([(1, S.RESUME_READY), (2, S.RESUME_READY), (3, S.APPLYING), (4, S.READY_FOR_REVIEW), (5, S.QUEUED)],
                          limit=4, target=79, start_blocked_ids=set(blocked))

    def test_missing_receipt_blocks_only_its_own_job(self):
        # 2026-10-15 user decision: a submission claim without receipt cannot cause a
        # duplicate at another posting, so the company's other jobs keep running.
        snapshot = {'config_hash': 'h', 'complete': False, 'target': 79, 'max_per_company': None,
                    'counts': {'submitted': 0, 'reserved': 0},
                    'errors': ['9:missing_submission_evidence', '9:submission_claim_requires_receipt'],
                    'dispatch_errors': ['9:missing_submission_evidence', '9:submission_claim_requires_receipt'],
                    'jobs': [job(9, 'elevenlabs'), job(10, 'elevenlabs'), job(11, 'acme')]}
        self.assertEqual(OrchestratorWorker._error_blocked(snapshot), {9})


if __name__ == '__main__':
    unittest.main()
