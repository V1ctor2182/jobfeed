from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline.answer_bank import AnswerBank
from pipeline.applier_discord import DiscordBridge
from tests.test_applier_discord import ALLOWED, FakeDiscord

JOBFEED = '''
CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT, real_job_id INTEGER);
CREATE TABLE real_jobs(id INTEGER PRIMARY KEY, representative_job_id INTEGER);
CREATE TABLE real_job_status_history(id INTEGER PRIMARY KEY, real_job_id INTEGER, from_status TEXT, to_status TEXT, reason TEXT);
CREATE TABLE application_queue(job_id INTEGER UNIQUE, state TEXT, manual_priority INTEGER, codex_application_session_id TEXT, hold TEXT);
CREATE TABLE pipeline_runs(id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, status TEXT, finished_at TEXT, jobs_discovered INTEGER,
  jobs_inserted INTEGER, jobs_updated INTEGER, jobs_scored INTEGER, total_llm_cost_usd REAL, failure_code TEXT, failure_message TEXT,
  scan_progress_json TEXT, verdict_counts_json TEXT);
'''
QUEUE = '''CREATE TABLE preparation_requests(request_id TEXT PRIMARY KEY, status TEXT, items TEXT, created_at TEXT, intent TEXT)'''


class JobfeedNoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / '.pipeline').mkdir()
        self.jobfeed = self.root / 'jobfeed.sqlite'
        with closing(sqlite3.connect(self.jobfeed)) as db, db:
            db.executescript(JOBFEED)
            for job in range(1, 121):
                db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (job, f'Co{job}', f'Engineer {job}', job))
                db.execute('INSERT INTO real_jobs VALUES(?,?)', (job, job))
        self.write(QUEUE, db=self.root / '.pipeline/dashboard-queue.sqlite3')
        self.discord = FakeDiscord()
        self.bridge = self.new_bridge()
        # History that is already there when notices start.
        self.history = 0
        self.move(1, None, 'new')
        self.move(2, 'scored', 'shortlisted')
        self.finish('old', 'all', 'succeeded', '2026-10-01T00:00:00Z')
        self.write('INSERT INTO application_queue(job_id,state,manual_priority) VALUES(1,"Queued",1)')
        self.handoff('old', 'preparing', [{'source_job_id': 1, 'status': 'queued'}], '2026-10-01T00:00:00+00:00')
        AnswerBank(self.root).mutate({'action': 'create', 'expected_revision': 0, 'draft': {'question': 'Old question?', 'answer': 'Old answer', 'scope': {'kind': 'global'}}})

    def new_bridge(self):
        return DiscordBridge(self.root, http=self.discord, channel_id='general', allowed_user_ids=[ALLOWED], jobfeed_db=self.jobfeed)

    def write(self, sql, args=(), db=None):
        with closing(sqlite3.connect(db or self.jobfeed)) as connection, connection:
            connection.execute(sql, args)

    def move(self, job, before, after, reason=None):
        self.history += 1
        self.write('INSERT INTO real_job_status_history VALUES(?,?,?,?,?)', (self.history, job, before, after, reason))

    def finish(self, run_id, source, status, finished_at, **counts):
        row = dict(run_id=run_id, source=source, status=status, finished_at=finished_at, jobs_discovered=0, jobs_inserted=0, jobs_updated=0,
                   jobs_scored=0, total_llm_cost_usd=0.0, scan_progress_json='{}', verdict_counts_json=None) | counts
        self.write(f"INSERT INTO pipeline_runs({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def handoff(self, request_id, status, items, created_at):
        self.write('INSERT INTO preparation_requests VALUES(?,?,?,?,?)', (request_id, status, json.dumps(items), created_at, 'handoff'),
                   db=self.root / '.pipeline/dashboard-queue.sqlite3')

    def notices(self):
        return [m['content'] for m in self.discord.messages.get('general', []) if m['author']['bot']]

    def test_history_is_not_posted_and_each_new_action_is_posted_once(self):
        self.bridge.sync()
        self.assertEqual(self.notices(), [])
        self.finish('scan', 'all', 'failed', '2026-10-27T15:33:06Z', jobs_discovered=614, jobs_inserted=356, jobs_updated=258,
                 failure_code='runtime_error', failure_message='Scan has failed source work',
                 scan_progress_json=json.dumps({'ats': {'phase': 'completed'}, 'handshake': {'phase': 'failed', 'message': 'HTTP 401'}}))
        self.finish('score', 'evaluate', 'succeeded', '2026-10-27T15:38:34Z', jobs_scored=83, total_llm_cost_usd=0.1998,
                 verdict_counts_json=json.dumps({'apply': 11, 'consider': 13, 'skip': 4}))
        self.finish('live', 'all', 'running', None)
        bank = AnswerBank(self.root)
        entry = bank.mutate({'action': 'create', 'expected_revision': 1, 'draft': {'question': 'Why us?', 'answer': 'I like the team.', 'scope': {'kind': 'global'}}})['entries'][-1]
        bank.mutate({'action': 'approve', 'expected_revision': 2, 'entry_id': entry['id'], 'expected_version': 1}, dashboard=True)
        bank.learn('Start date?', 'June 1', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        bank.mutate({'action': 'save', 'expected_revision': bank.read()['revision'], 'draft': {'question': 'GPA', 'answer': '3.5', 'scope': {'kind': 'global'}}}, dashboard=True)
        bank.decide('Any certifications?', 'N/A', {'kind': 'global'}, {'company': 'Co2', 'job_id': '2'}, owner='owner-2')
        self.write('INSERT INTO application_queue(job_id,state,manual_priority) VALUES(2,"Queued",1)')
        self.write('INSERT INTO application_queue(job_id,state,manual_priority) VALUES(3,"Queued",1)')
        self.handoff('new', 'starting', [{'source_job_id': 2, 'status': 'queued'}, {'source_job_id': 3, 'status': 'already_queued'},
                                         {'source_job_id': None, 'real_job_id': '9', 'status': 'rejected', 'reason': 'Only jobs currently in Wait can be prepared.'}],
                     '2026-10-27T16:50:33+00:00')
        self.move(3, 'scored', 'ignored')
        self.bridge.sync()
        self.bridge.sync()
        self.new_bridge().sync()
        self.assertEqual(self.notices(), [
            'Scan failed: 614 found, 356 new, 258 updated. Failed sources: handshake (HTTP 401).',
            'Scoring finished: 83 scored (Apply 11, Consider 13, Skip 4). Cost $0.20.',
            'Answer saved for review: Why us? → I like the team.\nAnswer approved: Why us? → I like the team.\nAnswer saved from your reply: Start date? → June 1\nAnswer saved: GPA → 3.5\nAnswer the Applier decided under auto submit: Any certifications? → N/A',
            'Added 2 jobs to the application queue with User priority: Co2 · Engineer 2, Co3 · Engineer 3.',
            'Sent 2 jobs to Apply: Co2 · Engineer 2, Co3 · Engineer 3.\n1 job not sent: Only jobs currently in Wait can be prepared.',
            'Ignored 1 job: Co3 · Engineer 3.',
        ])

    def test_a_bulk_move_is_one_message_and_automatic_moves_are_left_out(self):
        self.bridge.sync()
        self.bridge.thread_for(5)
        for job in range(10, 110):
            self.move(job, 'scored', 'shortlisted', 'bulk_selected')
        self.move(4, 'shortlisted', 'applied', 'record_application')
        self.move(5, 'shortlisted', 'applied', 'record_application')
        self.move(6, None, 'new')
        self.move(6, 'new', 'scored', 'auto_scored')
        self.bridge.sync()
        self.bridge.sync()
        self.assertEqual(self.notices()[1:], [
            'Moved 100 jobs to Wait: Co10 · Engineer 10, Co11 · Engineer 11, Co12 · Engineer 12, Co13 · Engineer 13, Co14 · Engineer 14, +95 more.\n'
            'Marked 1 job as applied: Co4 · Engineer 4.'])

    def test_a_handoff_waits_until_the_queue_has_decided(self):
        self.bridge.sync()
        self.handoff('pending', 'admitting', [{'source_job_id': 2, 'status': 'queued'}], '2026-10-27T16:00:00+00:00')
        self.bridge.sync()
        self.assertEqual(self.notices(), [])
        self.write("UPDATE preparation_requests SET status='queued' WHERE request_id='pending'", db=self.root / '.pipeline/dashboard-queue.sqlite3')
        self.bridge.sync()
        self.assertEqual(self.notices(), ['Sent 1 job to Apply: Co2 · Engineer 2.'])

    def test_an_unreadable_source_is_skipped_and_the_rest_still_post(self):
        self.write('DROP TABLE pipeline_runs')
        (self.root / '.pipeline/dashboard-queue.sqlite3').unlink()
        events = self.bridge.sync()
        self.assertEqual({e['source'] for e in events if e['event'] == 'notice_source_failed'}, {'runs', 'handoffs'})
        self.assertEqual([e for e in self.bridge.sync() if e['event'] == 'notice_source_failed'], [], 'a failure is reported once')
        self.move(2, 'shortlisted', 'ignored')
        self.bridge.sync()
        self.assertEqual(self.notices(), ['Ignored 1 job: Co2 · Engineer 2.'])

    def test_without_the_jobfeed_database_nothing_is_announced(self):
        bridge = DiscordBridge(self.root, http=self.discord, channel_id='general', allowed_user_ids=[ALLOWED])
        bridge.sync()
        self.move(2, 'shortlisted', 'ignored')
        bridge.sync()
        self.assertEqual(self.notices(), [])


if __name__ == '__main__':
    unittest.main()
