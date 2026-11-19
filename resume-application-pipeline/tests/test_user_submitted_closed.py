"""User reports prevent resubmission but require confirmation evidence for success."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import test_progress_monitor as fixture


class UserSubmittedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = fixture.Fixture(Path(tmp.name))

    def test_user_confirmed_synced_job_requires_confirmation_evidence(self):
        path = self.f.root / 'applications/one/user-submission-confirmation.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(job_id=1, status='submitted_user_confirmed',
                                        do_not_reapply=True, user_text='我交了', recorded_at=fixture.NOW)))
        with sqlite3.connect(self.f.db) as db:
            db.execute('INSERT INTO applied VALUES(1)')
        snapshot = self.f.monitor.tick(force=True, now=fixture.NOW)
        job = snapshot['jobs'][0]
        self.assertTrue(job['user_submitted'])
        self.assertFalse(job['submitted'])  # No receipt is fabricated.
        self.assertIn('missing_submission_evidence', job['errors'])
        self.assertIn('1:missing_submission_evidence', snapshot['errors'])
        self.assertEqual(job['blocked'], 'submission_unknown')
        self.assertFalse(job['closed'])


if __name__ == '__main__':
    unittest.main()
