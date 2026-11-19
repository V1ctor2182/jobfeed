import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.claude_cutover import drain, resume, status
from pipeline.store import ApplicationQueue
from test_claude_applier import FakeClaude

OWNER = 'd' * 32


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / 'batch.json'
        self.manifest.write_text(json.dumps({'ordered_candidates': [{'id': 1}], 'max_parallel': 1, 'application_model': 'x'}))
        self.db = self.root / 'jobfeed.sqlite3'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
            db.executemany('INSERT INTO jobs VALUES(?)', [(1,), (2,)])
        ApplicationQueue(self.db).initialize()
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO application_queue(job_id,state,codex_application_session_id) VALUES(1,'Applying',?)", (OWNER,))
            db.execute("INSERT INTO application_queue(job_id,state,codex_application_session_id) VALUES(2,'Applying','codex-thread')")
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        self.launcher.launch(1, scope='batch', url='https://x.test/1', materials=[], work_dir=self.root / 'w', launch_id=OWNER)
        p = patch('pipeline.claude_cutover.ClaudeApplierLauncher', side_effect=lambda root: self.launcher)
        p.start()
        self.addCleanup(p.stop)

    def test_drain_stops_waiting_appliers_keeps_owners_and_resume_lets_launches_start(self):
        before = self.manifest.read_text()
        self.launcher.store.ask(1, OWNER, 'question', 'Start date?')
        result = drain(self.root, self.db)
        self.assertEqual(self.manifest.read_text(), before)
        self.assertEqual(result['drain']['stopped'], [1])
        self.assertEqual(result['claude_jobs_kept'], [{'job_id': 1, 'state': 'Applying', 'owner': OWNER}])
        self.assertTrue(status(self.root, self.db)['draining'])
        resume(self.root)
        self.assertFalse(status(self.root, self.db)['draining'])

if __name__ == '__main__':
    unittest.main()
