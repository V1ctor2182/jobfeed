import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pipeline import tab_cleanup
from pipeline.applier_runtime import ApplierRuntime

FORM = 'https://alldus.example/apply/42'
LISTING = 'https://www.indeed.com/viewjob?jk=abc'


class FakeCdp:
    """A browser with fixed tabs. It records what was closed and what was attached."""

    def __init__(self, tabs):
        self.tabs, self.closed, self.calls = tabs, [], []

    def call(self, method, params=None, session=None):
        self.calls.append(method)
        if method == 'Target.getTargets':
            return {'targetInfos': [{'targetId': t['id'], 'type': t.get('type', 'page'), 'url': t['url']} for t in self.tabs]}
        if method == 'Target.attachToTarget':
            return {'sessionId': params['targetId']}
        if method == 'Page.getNavigationHistory':
            tab = next(t for t in self.tabs if t['id'] == session)
            if tab.get('unreadable'):
                raise RuntimeError('no history')
            return {'entries': [{'url': url} for url in tab.get('history', [tab['url']])]}
        if method == 'Target.closeTarget':
            self.closed.append(params['targetId'])
        return {}


class TabCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.jobfeed_db = self.root / 'jobfeed.sqlite3'
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE real_job_applications(source_job_id INTEGER PRIMARY KEY)')

    def job(self, job_id, *, launch='stopped', applied=False, urls=(FORM,)):
        self.store.bind(job_id, f'owner-{job_id}', 'batch')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE,'
                       ' config TEXT NOT NULL, background_id TEXT, session_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('INSERT INTO applier_launches VALUES(?,?,?,?,?,?,?,?,?)', (f'l-{job_id}', job_id, f'a-{job_id}', '{}', 'b', 's', launch, 'now', 'now'))
            for index, url in enumerate(urls):
                db.execute('INSERT INTO applier_tabs(job_id,tab_id,url,origin,recorded_at) VALUES(?,?,?,?,?)', (job_id, str(100 * job_id + index), url, 'o', 'now'))
        if applied:
            with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
                db.execute('INSERT INTO real_job_applications VALUES(?)', (job_id,))

    def sweep(self, tabs, **kwargs):
        cdp = FakeCdp(tabs)
        chosen = tab_cleanup.sweep(self.store, self.jobfeed_db, cdp, **kwargs)
        return cdp, [(page['id'], page['job_ids']) for page in chosen]

    def test_an_applied_jobs_tab_is_closed_once_its_applier_has_ended(self):
        self.job(1, applied=True, urls=(FORM, LISTING))
        cdp, chosen = self.sweep([{'id': 'a', 'url': LISTING}, {'id': 'b', 'url': FORM + '#step-2'}])
        self.assertEqual(chosen, [('a', [1]), ('b', [1])])
        self.assertEqual(cdp.closed, ['a', 'b'])

    def test_a_result_page_is_tied_to_its_job_through_the_tabs_history(self):
        self.job(1, applied=True)
        cdp, chosen = self.sweep([{'id': 'a', 'url': 'https://alldus.example/thank-you', 'history': [FORM, 'https://alldus.example/thank-you']}])
        self.assertEqual((chosen, cdp.closed), ([('a', [1])], ['a']))

    def test_a_tab_stays_while_its_job_is_in_progress_or_its_applier_still_runs(self):
        self.job(1, launch='parked')
        self.job(2, launch='running', applied=True, urls=('https://two.example/form',))
        self.job(3, launch='stopped', urls=('https://three.example/form',))
        cdp, chosen = self.sweep([{'id': 'a', 'url': FORM}, {'id': 'b', 'url': 'https://two.example/form'}, {'id': 'c', 'url': 'https://three.example/form'}])
        self.assertEqual((chosen, cdp.closed), ([], []), 'parked, still running, and ended without applying all keep their tabs')

    def test_a_tab_shared_with_an_unfinished_job_stays_open(self):
        self.job(1, applied=True)
        self.job(2, launch='parked')
        self.assertEqual(self.sweep([{'id': 'a', 'url': FORM}])[1], [])

    def test_a_tab_stays_when_an_unfinished_job_registered_the_same_page_without_its_tracking_query(self):
        posting = 'https://careers.icf.example/us/en/job/R2603410/Data-Engineer'
        self.job(4251, applied=True, urls=(posting + '?utm_source=linkedin',))
        self.job(4247, launch='parked', urls=(posting,))
        thanks = {'id': 'a', 'url': 'https://careers.icf.example/us/en/applythankyou?status=alreadyApplied', 'history': [posting + '?utm_source=linkedin']}
        cdp, chosen = self.sweep([thanks, {'id': 'b', 'url': posting + '?utm_source=linkedin'}])
        self.assertEqual((chosen, cdp.closed), ([], []))

    def test_pages_that_differ_only_in_what_their_query_names_are_different_pages(self):
        self.job(1, applied=True, urls=('https://www.indeed.com/viewjob?jk=aaa',))
        self.job(2, launch='parked', urls=('https://www.indeed.com/viewjob?jk=bbb',))
        self.assertEqual(self.sweep([{'id': 'a', 'url': 'https://www.indeed.com/viewjob?jk=aaa&from=serp'}, {'id': 'b', 'url': 'https://www.indeed.com/viewjob?jk=bbb'}])[1], [('a', [1])])
        self.assertEqual(self.sweep([{'id': 'a', 'url': 'https://www.indeed.com/viewjob?jk=aaa'}])[1], [('a', [1])])

    def test_a_tab_that_matches_no_job_is_never_closed(self):
        self.job(1, applied=True)
        cdp, chosen = self.sweep([{'id': 'a', 'url': 'https://mail.google.com/'}, {'id': 'b', 'url': 'chrome://newtab/'},
                                  {'id': 'w', 'url': FORM, 'type': 'service_worker'}])
        self.assertEqual((chosen, cdp.closed), ([], []))

    def test_a_tab_whose_history_cannot_be_read_is_judged_by_its_address(self):
        self.job(1, applied=True)
        self.assertEqual(self.sweep([{'id': 'a', 'url': FORM, 'unreadable': True}, {'id': 'b', 'url': 'https://x.example/', 'unreadable': True}])[1], [('a', [1])])

    def test_a_dry_run_lists_tabs_and_closes_none(self):
        self.job(1, applied=True)
        cdp, chosen = self.sweep([{'id': 'a', 'url': FORM}], dry_run=True)
        self.assertEqual((chosen, cdp.closed), ([('a', [1])], []))

    def test_a_skipped_or_store_submitted_job_also_counts_as_done(self):
        self.job(1)
        self.job(2, urls=('https://two.example/form',))
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("INSERT INTO applier_skips VALUES(1,'reason','dashboard','click-1','user','now')")
            db.execute("INSERT INTO applier_submits(job_id,state,attempts,requested_at) VALUES(2,'submitted',1,'now')")
        self.assertEqual(self.sweep([{'id': 'a', 'url': FORM}, {'id': 'b', 'url': 'https://two.example/form'}])[1], [('a', [1]), ('b', [2])])


    # 2026-11-16: tabs of jobs that ended without a Submit and went back to the queue, were cancelled, or closed, stayed
    # open for good, and so did the tabs of every requeued run, which the requeue had taken out of applier_tabs.

    def queue(self, job_id, state):
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS application_queue(job_id INTEGER PRIMARY KEY, state TEXT)')
            db.execute('INSERT OR REPLACE INTO application_queue VALUES(?,?)', (job_id, state))

    def test_an_ended_job_with_no_submit_record_that_left_the_work_closes_its_tabs(self):
        for job_id, state in ((1, 'Cancelled'), (2, 'Unavailable'), (3, 'Queued')):
            self.job(job_id, urls=(f'https://{job_id}.example/form',))
            self.queue(job_id, state)
        cdp, chosen = self.sweep([{'id': str(j), 'url': f'https://{j}.example/form'} for j in (1, 2, 3)])
        self.assertEqual(chosen, [('1', [1]), ('2', [2]), ('3', [3])])

    def test_a_job_still_applying_or_with_any_submit_record_keeps_its_tabs(self):
        self.job(1, urls=('https://one.example/form',))
        self.queue(1, 'Applying')
        self.job(2, urls=('https://two.example/form',))
        self.queue(2, 'Cancelled')
        self.job(3, launch='parked', urls=('https://three.example/form',))
        self.queue(3, 'Cancelled')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("INSERT INTO applier_submits(job_id,state,attempts,requested_at) VALUES(2,'unknown',1,'now')")
        tabs = [{'id': 'a', 'url': 'https://one.example/form'}, {'id': 'b', 'url': 'https://two.example/form'}, {'id': 'c', 'url': 'https://three.example/form'}]
        self.assertEqual(self.sweep(tabs)[1], [])

    def retire(self, job_id, url):
        with self.store._write() as db:
            db.execute('INSERT INTO applier_requeues VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (f'r-{job_id}', job_id, 'Applying', None, f'owner-{job_id}', '[]', 'worker', 'captcha', 'now',
                        json.dumps({'applier_tabs': [{'job_id': job_id, 'tab_id': '9', 'url': url}]})))

    def test_a_requeued_runs_archived_tabs_close_while_no_applier_runs_the_job(self):
        self.retire(7, LISTING)
        cdp, chosen = self.sweep([{'id': 'a', 'url': LISTING}])
        self.assertEqual((chosen, cdp.closed), ([('a', [7])], ['a']))

    def test_a_requeued_runs_tabs_wait_while_a_new_applier_runs_the_job(self):
        self.retire(7, LISTING)
        self.job(7, launch='running', urls=('https://seven.example/form',))
        self.assertEqual(self.sweep([{'id': 'a', 'url': LISTING}])[1], [])

    def test_a_requeued_runs_tab_stays_when_another_job_in_progress_registered_the_page(self):
        self.retire(7, LISTING)
        self.job(8, launch='parked', urls=(LISTING,))
        self.assertEqual(self.sweep([{'id': 'a', 'url': LISTING}])[1], [])

    def test_a_tab_is_tied_to_the_page_its_applier_registered_without_its_trailing_slash_or_tracking_query(self):
        self.job(1, applied=True, urls=('https://www.linkedin.com/jobs/view/4491056834',))
        cdp, chosen = self.sweep([{'id': 'a', 'url': 'https://www.linkedin.com/jobs/view/4491056834/?trk=x'}])
        self.assertEqual(chosen, [('a', [1])])


if __name__ == '__main__':
    unittest.main()
