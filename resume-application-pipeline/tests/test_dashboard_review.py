import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from pipeline import dashboard_review
from pipeline.applier_runtime import ApplierRuntime


class DashboardReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.store.bind(7, 'owner-7', 'batch')
        self.launch(7)
        self.jobfeed_db = self.root / 'jobfeed.sqlite3'
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT, url TEXT)')
            db.execute("INSERT INTO jobs VALUES(7,'Orchard','Platform engineer','https://example.test/7')")

    def launch(self, job_id, state='running'):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE,'
                       ' config TEXT NOT NULL, background_id TEXT, session_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO applier_launches VALUES(?,?,?,?,?,?,?,?,?)', (f'launch-{job_id}', job_id, f'applier-{job_id}', '{}', 'b', 's', state, 'now', 'now'))

    def ask(self, **fields):
        return self._ask({**dict(kind="question", text="What postal code?"), **fields})

    def _ask(self, fields):
        kind, text = fields.pop('kind'), fields.pop('text')
        result = self.store.ask(7, 'owner-7', kind, text, **fields)
        return result['id'] if isinstance(result, dict) else result

    def run_cli(self, command, payload):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), redirect_stdout(out):
            code = dashboard_review.main(['--root', str(self.root), '--jobfeed-db', str(self.jobfeed_db), command])
        return code, json.loads(out.getvalue())

    def test_list_shows_an_open_question_with_its_job_and_choices(self):
        request_id = self.ask(field='Postal Code', options=['11101', '11101-2910'])
        code, body = self.run_cli('list', {})
        self.assertEqual(code, 0)
        self.assertEqual([(i['id'], i['company'], i['title'], i['field'], i['options'], i['quick_replies']) for i in body['items']],
                         [(request_id, 'Orchard', 'Platform engineer', 'Postal Code', ['11101', '11101-2910'], [])])

    def test_a_question_whose_applier_ended_is_hidden_and_counted(self):
        self.ask()
        self.launch(7, state='stopped')
        self.assertEqual(self.run_cli('list', {})[1], {'items': [], 'ended': 1, 'sites': []})

    def test_questions_come_before_blockers(self):
        self.ask(kind='blocker', text='Needs a login.', blocker_type='user_only_check')
        self.ask(text='Which office?')
        self.assertEqual([item['kind'] for item in self.run_cli('list', {})[1]['items']], ['question', 'blocker'])

    def test_picking_an_option_saves_a_dashboard_reply_and_closes_the_question(self):
        request_id = self.ask(options=['11101', '11101-2910'])
        code, body = self.run_cli('reply', {'request_id': request_id, 'action': 'pick', 'text': '11101', 'click_id': 'click-1'})
        self.assertEqual((code, body['items']), (0, []))
        self.assertEqual(self.store.latest_reply(request_id), {'version': 1, 'text': '11101', 'source_kind': 'dashboard', 'intent': 'answer'})

    def test_a_pick_outside_the_offered_options_is_refused(self):
        request_id = self.ask(options=['11101', '11101-2910'])
        code, body = self.run_cli('reply', {'request_id': request_id, 'action': 'pick', 'text': '90210', 'click_id': 'click-1'})
        self.assertEqual((code, body['code']), (1, 'invalid'))
        self.assertIsNone(self.store.latest_reply(request_id))

    def test_approve_sends_the_draft_and_a_change_request_keeps_its_intent(self):
        first = self.ask(text='Why us?', draft='Because of the platform work.')
        self.run_cli('reply', {'request_id': first, 'action': 'approve', 'click_id': 'click-1'})
        self.assertEqual(self.store.latest_reply(first)['text'], 'Because of the platform work.')
        second = self.ask(text='Why this team?', draft='Draft two.')
        self.run_cli('reply', {'request_id': second, 'action': 'change_request', 'text': 'Shorter.', 'click_id': 'click-2'})
        self.assertEqual(self.store.latest_reply(second)['intent'], 'change_request')

    def test_a_blocker_takes_only_its_own_quick_replies_or_a_typed_answer(self):
        request_id = self.ask(kind='blocker', text='Did Submit go through?', blocker_type='submit_unknown')
        self.assertEqual(self.run_cli('list', {})[1]['items'], [], 'the confirmation email answers it, so the user is not asked')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE applier_requests SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (request_id,))
        self.assertEqual(self.run_cli('list', {})[1]['items'][0]['quick_replies'], ['submitted', 'not submitted'])
        code, _ = self.run_cli('reply', {'request_id': request_id, 'action': 'quick', 'text': 'done', 'click_id': 'click-1'})
        self.assertEqual(code, 1)
        code, _ = self.run_cli('reply', {'request_id': request_id, 'action': 'answer', 'text': 'I emailed them instead.', 'click_id': 'click-2'})
        self.assertEqual(code, 0)

    def test_a_closed_question_cannot_be_answered_again(self):
        request_id = self.ask()
        self.run_cli('reply', {'request_id': request_id, 'action': 'answer', 'text': '11101', 'click_id': 'click-1'})
        code, body = self.run_cli('reply', {'request_id': request_id, 'action': 'answer', 'text': '11102', 'click_id': 'click-2'})
        self.assertEqual((code, body['error']), (1, 'This question is already closed.'))

    def test_ignore_records_a_dashboard_skip_once(self):
        self.ask(kind='blocker', text='Needs a login.', blocker_type='user_only_check')
        code, body = self.run_cli('ignore', {'job_id': 7, 'click_id': 'click-1'})
        self.assertEqual((code, body['items']), (0, []))
        self.assertEqual((self.store.skip(7)['source_kind'], self.store.skip(7)['reason']), ('dashboard', 'Pressed Ignore in the Dashboard'))
        self.assertEqual(self.run_cli('ignore', {'job_id': 7, 'click_id': 'click-2'})[0], 1)


if __name__ == '__main__':
    unittest.main()
