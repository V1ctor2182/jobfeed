import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pipeline.worker import OrchestratorWorker, WorkerSettings

class DatabaseContentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.pipeline').mkdir()
        self.events = []
        self.worker = OrchestratorWorker(SimpleNamespace(config=SimpleNamespace(resume_root=self.root)),
            WorkerSettings(poll_interval=.01), emit=self.events.append)
        self.worker._user_stopped = Mock(return_value=False)
        self.addCleanup(self.worker.close)

    def test_real_writer_lock_release_recovers_once_without_losing_owner(self):
        db = self.root / 'queue.sqlite'
        owner = sqlite3.connect(db)
        other = sqlite3.connect(db, timeout=0)
        self.addCleanup(owner.close)
        self.addCleanup(other.close)
        owner.execute('CREATE TABLE queue (state TEXT)')
        owner.execute("INSERT INTO queue VALUES ('Tailoring')")
        owner.commit()
        owner.execute('BEGIN IMMEDIATE')
        calls = []
        existing_future = object()
        self.worker._application_futures = {1: ('existing', existing_future)}
        self.worker.close = Mock()  # Sentinel is not a real Future.
        def tick():
            calls.append(1)
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE queue SET state='Resume Ready'")
            other.commit()
            self.worker._progress_done = True
        self.worker.tick = tick
        with patch('pipeline.worker.time.sleep', side_effect=lambda _: owner.rollback()):
            self.worker.run_forever()
        self.assertEqual(len(calls), 2)
        self.assertEqual(other.execute('SELECT state FROM queue').fetchone()[0], 'Resume Ready')
        self.assertIs(self.worker._application_futures[1][1], existing_future)
        self.worker._application_futures.clear()
        self.assertEqual(sum(e['event']=='database_contention_recovered' for e in self.events), 1)

    def test_stop_during_lock_wait_prevents_next_tick(self):
        self.worker.tick = Mock(side_effect=sqlite3.OperationalError('database is locked'))
        self.worker._user_stopped = Mock(side_effect=[False, True])
        with patch('pipeline.worker.time.sleep') as sleep:
            self.worker.run_forever()
        self.assertEqual(self.worker.tick.call_count, 1)
        sleep.assert_not_called()

    def test_other_sqlite_errors_are_not_hidden(self):
        self.worker.tick = Mock(side_effect=sqlite3.OperationalError('no such table: queue'))
        with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
            self.worker.run_forever()
        self.assertEqual(self.worker.tick.call_count, 1)
