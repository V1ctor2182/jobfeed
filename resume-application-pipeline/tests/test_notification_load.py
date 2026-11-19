"""2026-10-15 user decision: wake the controller only for work the program cannot do."""
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_progress_monitor as fixture

T0 = '2026-10-13T18:00:00+00:00'
T1 = '2026-10-13T18:01:00+00:00'
T5 = '2026-10-13T18:05:00+00:00'
T16 = '2026-10-13T18:16:00+00:00'


class NotificationLoadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = fixture.Fixture(Path(tmp.name))

    def tick(self, at):
        self.f.monitor.tick(force=True, now=at)

    def events(self, kind=None, job=1):
        state = json.loads(self.f.monitor.path.read_text())
        return [e for e in state['events'].values() if (kind is None or e['kind'] == kind) and e['job_id'] == job]

    def pending(self, job=1):
        return [e for e in self.events(job=job) if e['delivery'] == 'pending']

    def stop(self, status, at=T0, job=1):
        """The Applier waits on the user: a check only the user can do, or a question."""
        kind, fields = ('blocker', dict(blocker_type='user_only_check')) if status == 'needs_verification' else ('question', {})
        self.f.ask(job, kind, 'Please complete the CAPTCHA', at=at, **fields)

    def test_success_is_recorded_without_waking_controller(self):
        self.tick(T0)
        self.f.receipt(success_evidence='Application submitted')
        self.tick(T1)
        self.assertEqual(len(self.events('新增提交成功')), 1)
        self.assertEqual(self.pending(), [])

    def test_sync_and_closeout_wait_for_the_program_first(self):
        self.tick(T0)
        self.f.receipt(success_evidence='Application submitted')
        self.tick(T1)
        self.tick(T5)
        self.assertEqual(self.pending(), [])
        self.assertIsNone(self.f.monitor.ledger.next(T5))
        self.tick(T16)  # Still unsynced after 15 minutes: now the controller is needed.
        self.assertEqual(self.f.monitor.ledger.next(T16)['kind'], 'sync')
        self.assertTrue(any(e['kind'] == '待办唤醒' for e in self.pending(job=None)))

    def test_same_observed_blocker_is_one_todo(self):
        self.tick(T0)
        self.stop('needs_verification')
        self.tick(T1)
        self.stop('needs_verification', at=T1)
        self.tick(T5)
        self.assertEqual(self.f.monitor.ledger.next(T5)['kind'], 'needs_verification')
        self.assertIsNone(self.f.monitor.ledger.next(T5))
        self.assertEqual(len(self.events('待办唤醒', job=None)), 1)

    def test_new_ambiguous_blocker_keeps_both_for_controller_identification(self):
        self.tick(T0)
        self.stop('needs_verification')
        self.tick(T1)
        self.stop('needs_input', at=T1)
        self.tick(T5)
        first = self.f.monitor.ledger.next(T5)
        second = self.f.monitor.ledger.next(T5)
        self.assertEqual(first['kind'], 'needs_verification')
        self.assertEqual(second['kind'], 'needs_input')
        self.assertNotEqual(first['issue_id'], second['issue_id'])
        self.assertTrue(first['identity_review_required'])
        self.assertTrue(second['identity_review_required'])
        self.assertEqual(self.f.monitor.ledger.parked(), [])
        self.assertIsNone(self.f.monitor.ledger.next(T5))

if __name__ == '__main__':
    unittest.main()
