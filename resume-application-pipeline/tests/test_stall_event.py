import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import test_progress_monitor as fixture

T0 = '2026-10-13T18:00:00+00:00'
T9 = '2026-10-13T18:09:00+00:00'
T11 = '2026-10-13T18:11:00+00:00'
T20 = '2026-10-13T18:20:00+00:00'


class StallEventTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = fixture.Fixture(Path(tmp.name))
        # One more queued job that no worker has dispatched yet.
        self.f.data['ordered_candidates'].append(dict(id=3, company='Third', title='Engineer'))
        self.f.manifest.write_text(json.dumps(self.f.data))
        with sqlite3.connect(self.f.db) as db:
            db.execute("INSERT INTO jobs(id,url) VALUES(3,'https://example.com/jobs/3')")

    def end_all_turns(self):
        for i in (1, 2):
            self.f.step(i, 'ready for review')

    def stalls(self):
        state = json.loads(self.f.monitor.path.read_text())
        return [i for i in self.f.monitor.ledger.board()['queue'] if i['kind'] == 'system' and 'dispatch_stalled_since' in str(i['evidence'])]

    def test_idle_batch_with_queue_emits_one_stall_event(self):
        self.end_all_turns()
        for at in (T0, T9):
            self.f.monitor.tick(force=True, now=at)
        self.assertEqual(self.stalls(), [])
        self.f.monitor.tick(force=True, now=T11)
        self.f.monitor.tick(force=True, now=T20)
        self.assertEqual(len(self.stalls()), 1)
        self.assertEqual(self.stalls()[0]['status'], 'queued')

    def test_running_agent_is_not_a_stall(self):
        for at in (T0, T11, T20):
            self.f.monitor.tick(force=True, now=at)
        self.assertEqual(self.stalls(), [])


if __name__ == '__main__':
    unittest.main()
