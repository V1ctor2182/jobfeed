import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pipeline import dashboard_review, site_holds
from pipeline.applier_runtime import ApplierRuntime
from pipeline.autopilot import startable
from pipeline.lifecycle import ApplicationState as S
from pipeline.store import ApplicationQueue

SIGN_IN = 'https://secure.indeed.com/auth'


class SiteHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobfeed_db = self.root / 'jobfeed.sqlite3'
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, platform TEXT, company TEXT, title TEXT, url TEXT)')
        self.queue = ApplicationQueue(self.jobfeed_db)
        self.queue.initialize()
        self.store = ApplierRuntime(self.root)
        self.pipeline = SimpleNamespace(queue=self.queue, config=SimpleNamespace(jobfeed_db=self.jobfeed_db, resume_root=self.root),
                                        autopilot_sheet_read=(float('inf'), set()))

    def job(self, job_id, platform, *, queued=True):
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', (job_id, platform, f'Company {job_id}', 'Engineer', f'https://example.test/{job_id}'))
        if queued:
            self.queue.enqueue(job_id)

    def stopped(self, job_id, platform='indeed', page_url=SIGN_IN, state='parked'):
        """A job whose Applier reported a sign-in it cannot do."""
        self.job(job_id, platform, queued=False)
        self.store.bind(job_id, f'owner-{job_id}', 'batch')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE,'
                       ' config TEXT NOT NULL, background_id TEXT, session_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('INSERT INTO applier_launches VALUES(?,?,?,?,?,?,?,?,?)', (f'l-{job_id}', job_id, f'a-{job_id}', '{}', 'b', 's', state, 'now', 'now'))
        result = self.store.ask(job_id, f'owner-{job_id}', 'blocker', 'The page needs a sign-in.', blocker_type='user_only_check', evidence={'page_url': page_url})
        return result['id'] if isinstance(result, dict) else result

    def test_three_appliers_stopped_on_a_boards_own_page_hold_that_board(self):
        for job_id in (1, 2):
            self.stopped(job_id)
        self.assertEqual(site_holds.park(self.pipeline, self.store), [])
        self.stopped(3)
        self.assertEqual(site_holds.park(self.pipeline, self.store), [{'event': 'site_held', 'site': 'indeed', 'blocked': 3}])
        self.assertEqual(site_holds.park(self.pipeline, self.store), [], 'an active hold is raised once')
        self.assertEqual(list(site_holds.active(self.store)), ['indeed'])

    def test_old_stops_do_not_hold_a_board_that_has_worked_since(self):
        from datetime import datetime, timedelta, timezone
        for job_id in (1, 2, 3):
            self.stopped(job_id)
        later = datetime.now(timezone.utc) + site_holds.RECENT + timedelta(minutes=1)
        self.assertEqual(site_holds.park(self.pipeline, self.store, now=later), [])
        self.assertEqual(site_holds.active(self.store), {})
        group = site_holds.groups(self.store, self.jobfeed_db)
        self.assertEqual([(g['site'], g['held'], len(g['request_ids'])) for g in group], [('indeed', False, 3)], 'they can still be retried together')
        self.assertIsNotNone(group[0]['latest_stop_at'])

    def test_a_held_boards_waiting_jobs_do_not_start_while_started_jobs_and_other_boards_go_on(self):
        for job_id, platform in ((10, 'indeed'), (11, 'greenhouse'), (12, 'indeed')):
            self.job(job_id, platform)
        self.queue.transition(12, S.RESUME_READY)
        self.queue.transition(12, S.APPLYING, codex_application_session_id='owner-12')
        items = self.queue.list_active()
        self.assertEqual(sorted(i.job_id for i in startable(self.pipeline, items)), [10, 11, 12])
        site_holds.hold(self.store, 'indeed', 'sign-in', 'test')
        self.assertEqual(sorted(i.job_id for i in startable(self.pipeline, items)), [11, 12])
        self.assertEqual(site_holds.held_jobs(self.store, self.jobfeed_db, [10, 11]), {10})
        self.assertTrue(site_holds.clear(self.store, 'indeed'))
        self.assertEqual(sorted(i.job_id for i in startable(self.pipeline, items)), [10, 11, 12])

    def test_a_sign_in_on_an_employers_site_or_from_an_ended_applier_does_not_count(self):
        self.stopped(1, page_url='https://careers.microsoft.com/login')
        self.stopped(2, page_url='https://jobs.siemens.com/login')
        self.stopped(3, page_url='https://notindeed.example.com/login')
        self.stopped(4, state='stopped')
        self.stopped(5)
        self.assertEqual({site: len(found) for site, found in site_holds.blocked_sites(self.store, self.jobfeed_db).items()}, {'indeed': 1})
        self.assertEqual(site_holds.park(self.pipeline, self.store), [])

    def test_resume_clears_the_hold_and_tells_each_stopped_applier_to_try_again(self):
        requests = [self.stopped(job_id) for job_id in (1, 2, 3)]
        self.job(10, 'indeed')
        site_holds.park(self.pipeline, self.store)
        group = site_holds.groups(self.store, self.jobfeed_db)
        self.assertEqual([(g['site'], g['label'], g['held'], g['job_ids'], g['queued'], g['page_url']) for g in group],
                         [('indeed', 'Sign in to Indeed', True, [1, 2, 3], 1, SIGN_IN)])
        result = site_holds.resume(self.store, self.jobfeed_db, 'indeed', click_id='click-1', sender='dashboard-user')
        self.assertEqual(result, {'site': 'indeed', 'cleared': True, 'woken': 3})
        self.assertEqual([self.store.latest_reply(r)['text'] for r in requests], ['done'] * 3)
        self.assertEqual({self.store.latest_reply(r)['source_kind'] for r in requests}, {'dashboard'})
        self.assertEqual(site_holds.groups(self.store, self.jobfeed_db), [])
        self.assertEqual(site_holds.held_jobs(self.store, self.jobfeed_db, [10]), set())

    def test_resuming_a_board_that_is_neither_held_nor_blocked_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'not held'):
            site_holds.resume(self.store, self.jobfeed_db, 'indeed', click_id='click-1', sender='dashboard-user')

    def test_the_review_program_lists_the_board_and_resumes_it(self):
        for job_id in (1, 2, 3):
            self.stopped(job_id)
        site_holds.park(self.pipeline, self.store)

        def run(command, payload):
            out = io.StringIO()
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), redirect_stdout(out):
                code = dashboard_review.main(['--root', str(self.root), '--jobfeed-db', str(self.jobfeed_db), command])
            return code, json.loads(out.getvalue())
        code, body = run('list', {})
        self.assertEqual((code, [(s['site'], s['held'], len(s['request_ids'])) for s in body['sites']]), (0, [('indeed', True, 3)]))
        code, body = run('resume-site', {'site': 'indeed', 'click_id': 'click-1'})
        self.assertEqual((code, body['sites'], body['items']), (0, [], []))
        self.assertEqual(run('resume-site', {'site': 'indeed', 'click_id': 'click-2'})[0], 1)


if __name__ == '__main__':
    unittest.main()
