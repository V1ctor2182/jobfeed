import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from pipeline.applier_runtime import ApplierRuntime


def write_from_job(root, job_id, start, output):
    try:
        store = ApplierRuntime(root)
        start.wait(10)
        for n in range(20):
            store.progress(job_id, f'owner-{job_id}', f'step {n}')
            store.ask(job_id, f'owner-{job_id}', 'question', f'question {n} for job {job_id}')
        output.put('ok')
    except BaseException as exc:
        output.put(type(exc).__name__ + ': ' + str(exc))


CRASHED_WRITER = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=1")
db.execute("BEGIN")
db.execute("UPDATE applier_requests SET state='crashed'")
db.execute("CREATE TABLE junk(x)")
db.execute("INSERT INTO junk WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<300) SELECT randomblob(2000) FROM c")
os._exit(1)
"""


class ApplierRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.store.bind(1, 'owner-1', 'batch')

    def test_a_write_cut_off_by_a_crash_is_rolled_back_before_reading(self):
        request = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        subprocess.run([sys.executable, '-c', CRASHED_WRITER, str(self.store.path)], check=False)
        self.assertTrue(Path(f'{self.store.path}-journal').exists())
        self.assertEqual(self.store.request(request)['state'], 'open')
        self.assertFalse(Path(f'{self.store.path}-journal').exists())

    def test_one_owner_per_job(self):
        self.store.bind(1, 'owner-1', 'batch')
        with self.assertRaisesRegex(ValueError, 'already has an owner'):
            self.store.bind(1, 'owner-2', 'batch')
        with self.assertRaisesRegex(ValueError, 'another job'):
            self.store.bind(2, 'owner-1', 'batch')
        with self.assertRaisesRegex(ValueError, 'does not own'):
            self.store.ask(1, 'owner-2', 'question', 'What is your start date?')

    def test_only_a_question_offers_options_and_they_must_fit_on_buttons(self):
        plain = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        self.assertIsNone(self.store.request(plain)['options'])
        chosen = self.store.ask(1, 'owner-1', 'question', 'Do you need sponsorship?', options=[' Yes ', 'No'])
        self.assertEqual(self.store.request(chosen)['options'], ['Yes', 'No'])
        for options in (['Yes'], ['Yes', 'Yes '], ['Yes', 'x' * 81], ['Yes', ''], [str(n) for n in range(21)]):
            with self.assertRaisesRegex(ValueError, 'options must be'):
                self.store.ask(1, 'owner-1', 'question', 'Another question?', options=options)
        with self.assertRaisesRegex(ValueError, 'only a question'):
            self.store.ask(1, 'owner-1', 'blocker', 'Stuck', blocker_type='tab_missing', options=['Yes', 'No'])

    def test_repeated_question_returns_the_same_request(self):
        first = self.store.ask(1, 'owner-1', 'question', 'What is  your start date?')
        again = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        other = self.store.ask(1, 'owner-1', 'question', 'Do you need sponsorship?')
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_blocker_needs_a_known_type(self):
        with self.assertRaisesRegex(ValueError, 'blocker_type'):
            self.store.ask(1, 'owner-1', 'blocker', 'Location picker is stuck')
        with self.assertRaisesRegex(ValueError, 'blocker_type'):
            self.store.ask(1, 'owner-1', 'blocker', 'Location picker is stuck', blocker_type='flaky')
        request = self.store.ask(1, 'owner-1', 'blocker', 'Location picker is stuck', field='location',
                                 blocker_type='broken_component', evidence={'attempts': ['form_input', 'computer', 'javascript_tool']})
        self.assertTrue(request)

    def test_replay_of_a_reply_is_a_no_op_and_rewrite_is_rejected(self):
        request = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        self.assertEqual(self.store.reply(request, 'June 1', source_kind='discord', source_id='msg-1', sender_id='user-1'), 1)
        self.assertEqual(self.store.reply(request, 'June 1', source_kind='discord', source_id='msg-1', sender_id='user-1'), 1)
        with self.assertRaisesRegex(ValueError, 'cannot be rewritten'):
            self.store.reply(request, 'July 1', source_kind='discord', source_id='msg-1', sender_id='user-1')
        self.assertEqual(self.store.reply(request, 'July 1', source_kind='discord', source_id='msg-2', sender_id='user-1'), 2)
        self.assertEqual(self.store.latest_reply(request)['text'], 'July 1')

    def test_a_store_made_before_reply_intents_reads_old_replies_as_answers_and_takes_change_requests(self):
        request = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', draft='I like mortgages.')
        with sqlite3.connect(self.store.path) as db:
            db.execute('ALTER TABLE applier_replies DROP COLUMN intent')
            db.execute("INSERT INTO applier_replies VALUES(?,1,'Old answer','discord','msg-old','user-1','2026-01-01T00:00:00+00:00')", (request,))
        reopened = ApplierRuntime(self.root)
        self.assertEqual(reopened.latest_reply(request), {'version': 1, 'text': 'Old answer', 'source_kind': 'discord', 'intent': 'answer'})
        self.assertEqual(reopened.reply(request, 'Shorter', source_kind='discord', source_id='msg-2', sender_id='user-1', intent='change_request'), 2)
        self.assertEqual(reopened.reply(request, 'Shorter', source_kind='discord', source_id='msg-2', sender_id='user-1', intent='change_request'), 2)
        with self.assertRaisesRegex(ValueError, 'cannot be rewritten'):
            reopened.reply(request, 'Shorter', source_kind='discord', source_id='msg-2', sender_id='user-1')
        self.assertEqual(reopened.latest_reply(request)['intent'], 'change_request')

    def test_only_a_question_with_a_draft_takes_a_change_request(self):
        plain = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        with self.assertRaisesRegex(ValueError, 'with a draft'):
            self.store.reply(plain, 'Shorter', source_kind='discord', source_id='msg-1', sender_id='user-1', intent='change_request')
        with self.assertRaisesRegex(ValueError, 'unknown reply intent'):
            self.store.reply(plain, 'June 1', source_kind='discord', source_id='msg-1', sender_id='user-1', intent='approve')
        self.assertIsNone(self.store.latest_reply(plain))

    def test_a_new_draft_after_a_change_request_opens_a_new_question(self):
        first = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='Draft one.', company='Valon')
        self.store.reply(first, 'Shorter', source_kind='discord', source_id='msg-1', sender_id='user-1', intent='change_request')
        self.assertEqual(self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='Draft one.'), first)
        second = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='Draft two.', company='Valon')
        self.assertNotEqual(second, first)
        self.assertEqual((self.store.request(first)['state'], self.store.request(second)['state']), ('redrafted', 'open'))
        self.assertEqual(self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='Draft one.'), second)
        self.store.reply(second, 'Draft two.', source_kind='discord', source_id='msg-2', sender_id='user-1')
        self.assertEqual(self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='Draft three.'), second)
        feedback = self.store.draft_feedback()
        self.assertEqual([{k: row[k] for k in ('feedback', 'question', 'field', 'draft', 'company', 'job_id')} for row in feedback],
                         [{'feedback': 'Shorter', 'question': 'Why Valon?', 'field': 'Why Valon', 'draft': 'Draft one.', 'company': 'Valon', 'job_id': 1}])

    def test_readback_rejects_a_stale_reply_version(self):
        request = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        self.store.reply(request, 'June 1', source_kind='discord', source_id='msg-1', sender_id='user-1')
        self.store.reply(request, 'July 1', source_kind='dashboard', source_id='event-2', sender_id='user-1')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.readback(1, 'owner-1', 'start_date', 'June 1', request_id=request, reply_version=1)
        self.assertTrue(self.store.readback(1, 'owner-1', 'start_date', 'July 1', request_id=request, reply_version=2))

    def test_wait_returns_when_a_reply_arrives_and_none_when_stopped(self):
        request = self.store.ask(1, 'owner-1', 'question', 'What is your start date?')
        stopped = threading.Event()
        stopped.set()
        self.assertIsNone(self.store.wait_for_reply(request, poll=0.01, stop=stopped))
        timer = threading.Timer(0.2, self.store.reply, (request, 'June 1'), dict(source_kind='discord', source_id='msg-1', sender_id='user-1'))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self.store.wait_for_reply(request, poll=0.02)['text'], 'June 1')

    def test_tabs_keep_native_ids_and_origins(self):
        self.store.register_tab(1, 'owner-1', 1778002301, 'http://127.0.0.1:8790/job1.html')
        self.store.register_tab(1, 'owner-1', 1778002301, 'http://127.0.0.1:8790/job1.html#q')
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            self.store.register_tab(1, 'owner-1', '1778002301', 'http://127.0.0.1:8790/job1.html')
        with self.assertRaisesRegex(ValueError, 'http'):
            self.store.register_tab(1, 'owner-1', 7, 'chrome://extensions')

    def test_notes_are_shared_by_site(self):
        self.store.note(1, 'owner-1', 'workday', 'country picker', 'form_input failed, click then type worked')
        self.assertEqual(self.store.notes('workday'), [{'widget': 'country picker', 'text': 'form_input failed, click then type worked'}])
        self.assertEqual(self.store.notes('greenhouse'), [])

    def test_different_jobs_write_concurrently(self):
        for job in (2, 3, 4):
            self.store.bind(job, f'owner-{job}', 'batch')
        context = multiprocessing.get_context('spawn')
        start, output = context.Event(), context.Queue()
        workers = [context.Process(target=write_from_job, args=(self.root, job, start, output)) for job in (2, 3, 4)]
        for worker in workers:
            worker.start()
        start.set()
        results = [output.get(timeout=60) for _ in workers]
        for worker in workers:
            worker.join(10)
        self.assertEqual(results, ['ok', 'ok', 'ok'])
        with sqlite3.connect(self.store.path) as db:
            counts = db.execute('SELECT job_id,COUNT(*) FROM applier_requests GROUP BY job_id ORDER BY job_id').fetchall()
        self.assertEqual(counts, [(2, 20), (3, 20), (4, 20)])


if __name__ == '__main__':
    unittest.main()
