import unittest
from pipeline.lifecycle import ApplicationState as S
from batch_stub import batch_stub

class BatchTest(unittest.TestCase):
    def make(self, states, confirmed=()):
        return batch_stub(states, limit=3, target=2, batch_ids=(1,2,3,4,5), confirmed=confirmed)
    def test_reserved_applications_cannot_overshoot_target(self):
        p=self.make([(2,S.APPLYING),(3,S.RESUME_READY)],confirmed=(1,))
        self.assertFalse(p.can_start_application())

