from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from pipeline.submission_reservation import reservations

CRASHED_WRITER = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=1")
db.execute("BEGIN")
db.execute("UPDATE requests SET state='crashed'")
db.execute("CREATE TABLE junk(x)")
db.execute("INSERT INTO junk WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<300) SELECT randomblob(2000) FROM c")
os._exit(1)
"""


class SubmissionReservationTests(unittest.TestCase):
    def test_a_write_cut_off_by_a_restart_does_not_stop_the_worker_reading_reservations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.pipeline/dashboard-submissions.sqlite3'
            path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE requests(id TEXT, state TEXT, prompt TEXT, preview TEXT)')
                db.execute("INSERT INTO requests VALUES('r1', 'queued', NULL, '{}')")
            subprocess.run([sys.executable, '-c', CRASHED_WRITER, str(path)], check=False)
            self.assertTrue(Path(f'{path}-journal').exists())
            self.assertEqual([row['state'] for row in reservations(temp)], ['queued'])
            self.assertFalse(Path(f'{path}-journal').exists())

    def test_rows_leave_out_the_pinned_manifest_and_match_their_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.pipeline/dashboard-submissions.sqlite3'
            path.parent.mkdir()
            manifest = '{"ordered_candidates": [' + ','.join(['{"id": 1}'] * 1000) + ']}'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE requests(id TEXT, job INTEGER, state TEXT, prompt TEXT, preview TEXT, pinned TEXT)')
                for request, scope, state in (('a', 'one', 'queued'), ('b', 'two', 'sync_pending'), ('c', 'one', 'submitted')):
                    db.execute('INSERT INTO requests VALUES(?,1,?,NULL,?,?)', (request, state, f'{{"scope": "{scope}", "manifest": {manifest}}}', manifest))
            self.assertEqual(reservations(temp, scope='one'), [{'id': 'a', 'job': 1, 'state': 'queued', 'prompt': None}])
            self.assertEqual([row['id'] for row in reservations(temp)], ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
