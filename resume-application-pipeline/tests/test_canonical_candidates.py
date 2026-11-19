import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline.jobfeed import Jobfeed


def card(i, **extra):
    return dict(source_job_id=str(i), real_job_id=str(i), verdict='apply',
                decision='results', identity_review_state='clear', evaluation_stale_reason=None,
                closed_at=None, **extra)

class CanonicalCandidatesTest(unittest.TestCase):
    def test_official_order_verdict_pagination_and_queue(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'db'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,real_job_id INTEGER,company TEXT,title TEXT,jd_text TEXT,url TEXT); CREATE TABLE application_queue(job_id INTEGER); CREATE TABLE applied(job_id INTEGER);')
                for i in range(1,9):
                    c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(i,i,'Company','SWE','Full JD','https://example.test'))
                c.execute('INSERT INTO application_queue VALUES(3)')
            rows=[card(1),card(2),card(3),card(4),card(5),card(6)]
            rows[0]['verdict']='skip';rows[1]['evaluation_stale_reason']='policy_changed';rows[3]['identity_review_state']='pending'
            feed=Jobfeed(db,Path(root))
            with patch.object(feed,'_canonical_page',side_effect=[{'jobs':rows,'total':8},{'jobs':[card(8),card(7)],'total':8}]) as page:
                self.assertEqual([j.id for j in feed.priority_jobs(limit=4)],[4,5,6,8])
                self.assertEqual(page.call_args_list[1].args,(6,))

    def test_service_failure_never_falls_back_to_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'db'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE jobs(id INTEGER,real_job_id INTEGER); CREATE TABLE application_queue(job_id INTEGER);')
            feed=Jobfeed(db,Path(root))
            with patch.object(feed,'_canonical_page',side_effect=RuntimeError('service down')):
                with self.assertRaisesRegex(RuntimeError,'service down'):
                    feed.priority_jobs()

    def test_public_results_request_uses_canonical_filters_and_sort(self):
        import io,json,os
        from urllib.parse import urlparse,parse_qs
        with patch.dict(os.environ), patch('pipeline.jobfeed.urlopen',return_value=io.BytesIO(json.dumps({'jobs':[],'total':0}).encode())) as request:
            os.environ.pop('JOBFEED_API_URL',None)
            Jobfeed(Path('/unused'),Path('/unused'))._canonical_page(100)
        parsed=urlparse(request.call_args.args[0])
        self.assertEqual(parsed.netloc,'127.0.0.1:8765')
        self.assertEqual(parsed.path,'/api/real-jobs')
        self.assertEqual(parse_qs(parsed.query),dict(decision=['results'],require_verdict=['true'],sort=['triage_score_desc'],limit=['100'],offset=['100']))

    def test_verdicts_choose_which_stage_b_verdicts_are_eligible(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'db'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,real_job_id INTEGER,company TEXT,title TEXT,jd_text TEXT,url TEXT); CREATE TABLE application_queue(job_id INTEGER); CREATE TABLE applied(job_id INTEGER);')
                for i in range(1,4):
                    c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(i,i,'Company','SWE','Full JD','https://example.test'))
            rows=[card(1),card(2),card(3)]
            rows[1]['verdict']='consider';rows[2]['verdict']='skip'
            feed=Jobfeed(db,Path(root))
            with patch.object(feed,'_canonical_page',return_value={'jobs':rows,'total':3}):
                self.assertEqual([j.id for j in feed.priority_jobs(limit=3)],[1])
                self.assertEqual([j.id for j in feed.priority_jobs(limit=3,verdicts=frozenset({'apply','consider'}))],[1,2])
