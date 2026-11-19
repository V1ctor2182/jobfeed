import unittest,sqlite3,tempfile
from pathlib import Path
from pipeline.jobfeed import Jobfeed
from pipeline.results import parse_application_result
class LegacyCleanupTest(unittest.TestCase):
 def test_compact_chinese_result_is_parsed(self):
  self.assertEqual(parse_application_result('申请进度：提交结果待确认说明：官网没有回执')['status'],'submission_unknown')
 def test_results_excludes_only_exact_queued_source_and_ignores_legacy_status(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'db';c=sqlite3.connect(p)
   c.executescript('''CREATE TABLE real_jobs(id INTEGER PRIMARY KEY,representative_job_id INTEGER,official_closed_at TEXT);
   CREATE TABLE jobs(id INTEGER PRIMARY KEY,real_job_id INTEGER,company TEXT,title TEXT,jd_text TEXT,url TEXT,platform TEXT,closed_at TEXT,posted_at TEXT,discovered_at TEXT);
   CREATE TABLE applied(job_id INTEGER); CREATE TABLE real_job_applications(real_job_id INTEGER,source_job_id INTEGER,source_applied_job_id INTEGER);
   CREATE TABLE application_queue(job_id INTEGER); CREATE TABLE job_priority_snapshot(job_id INTEGER,priority_score REAL);
   CREATE TABLE job_status(job_id INTEGER,status TEXT); CREATE TABLE real_job_status(real_job_id INTEGER,status TEXT);
   INSERT INTO real_jobs VALUES(10,1,NULL),(20,3,NULL);
   INSERT INTO jobs VALUES(1,10,'A','SWE','JD','https://a','company',NULL,NULL,NULL),(2,10,'A','SWE','JD','https://b','linkedin',NULL,NULL,NULL),(3,20,'B','SWE','JD','https://c','lever',NULL,NULL,NULL);
   INSERT INTO job_status VALUES(1,'rejected'); INSERT INTO application_queue VALUES(2);''');c.commit();c.close()
   from unittest.mock import patch
   from test_canonical_candidates import card
   feed=Jobfeed(p,Path(d));rows=[card(1),card(3)]
   rows[0]['real_job_id']='10';rows[1]['real_job_id']='20'
   with patch.object(feed,'_canonical_page',return_value={'jobs':rows,'total':2}):
    self.assertEqual([j.id for j in feed.priority_jobs()],[1,3])
    with sqlite3.connect(p) as c:
     c.execute('INSERT INTO application_queue VALUES(1)')
    self.assertEqual([j.id for j in feed.priority_jobs()],[3])
