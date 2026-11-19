import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pipeline.config import PipelineConfig
from pipeline.submission_release import grant_release, permits, path_for


class SubmissionReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = PipelineConfig.discover(Path(self.temp.name))
        self.context = {'scope':'/scope','job':1,'owner':'owner','controller':'controller','snapshot':'original','project':'/project'}
        self.stack = []
        for name, value in [('context', self.context), ('worker_stopped', nullcontext()),
                            ('read_thread', {'id':'owner','cwd':'/project'}), ('host_turn_state','active')]:
            p = patch('pipeline.submission_release.'+name, return_value=value)
            self.stack.append(p.start())
            self.addCleanup(p.stop)
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID':'controller'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def grant(self):
        return grant_release(self.config,'/scope',1,'owner','turn')

    def test_exact_live_turn_only_and_replay_does_not_extend_expiry(self):
        first = self.grant()
        self.assertTrue(permits(self.config,'owner','turn'))
        self.assertFalse(permits(self.config,'owner','other-turn'))
        self.assertFalse(permits(self.config,'other-owner','turn'))
        self.assertFalse(permits(self.config,'owner','turn',scope='/other',job=1))
        self.assertFalse(permits(self.config,'owner','turn',scope='/scope',job=2))
        self.assertEqual(self.grant(),first)

    def test_other_controller_cannot_grant(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'applier'}):
            with self.assertRaisesRegex(ValueError,'only the authorized controller'):
                self.grant()
        self.assertFalse(path_for(self.config,'owner').exists())

    def test_new_stop_auth_or_answer_inventory_invalidates_without_mutation(self):
        self.grant()
        before=path_for(self.config,'owner').read_bytes()
        self.stack[0].return_value=self.context | {'snapshot':'changed'}
        self.assertFalse(permits(self.config,'owner','turn'))
        self.assertEqual(path_for(self.config,'owner').read_bytes(),before)

    def test_owner_or_submission_state_rejection_fails_closed(self):
        self.grant()
        self.stack[0].side_effect=ValueError('owner changed or submission unknown')
        self.assertFalse(permits(self.config,'owner','turn'))

    def test_terminal_turn_is_not_reusable(self):
        self.grant()
        self.stack[3].return_value='completed'
        self.assertFalse(permits(self.config,'owner','turn'))
        with self.assertRaisesRegex(ValueError,'exact live owner turn'):
            self.grant()

    def test_expired_or_malformed_permit_fails_closed(self):
        release=self.grant()
        p=path_for(self.config,'owner')
        release['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        p.write_text(json.dumps(release))
        self.assertFalse(permits(self.config,'owner','turn'))
        p.write_text('{')
        self.assertFalse(permits(self.config,'owner','turn'))

    def test_active_worker_blocks_permission(self):
        self.grant()
        self.stack[1].side_effect=ValueError('worker active')
        self.assertFalse(permits(self.config,'owner','turn'))


if __name__ == '__main__':
    unittest.main()
