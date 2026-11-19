import sqlite3
import tempfile
import unittest
from pathlib import Path
from pipeline.config import PipelineConfig
from pipeline.jobfeed import Jobfeed
from pipeline.store import ApplicationQueue
from pipeline.orchestrator import ApplicationPipeline
from pipeline.lifecycle import ApplicationState as S

class QueueFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / 'jobs.sqlite'
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,real_job_id INTEGER,company TEXT,title TEXT,jd_text TEXT,url TEXT); CREATE TABLE applied(job_id INTEGER);')
            for i in range(1,120):
                c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(i,i,'Company','Engineer','JD '+str(i),'https://example.test/'+str(i)))
        self.queue = ApplicationQueue(self.db)
        self.queue.initialize()
        self.feed = Jobfeed(self.db,root)
        self.pipeline = ApplicationPipeline(PipelineConfig(root,root,self.db,root/'applications'),queue=self.queue,jobfeed=self.feed)

class DurableTerminalTest(unittest.TestCase):
    setUp=QueueFixture.setUp
    def test_cancel_survives_restart_and_cannot_reenqueue(self):
        self.queue.enqueue(1)
        self.queue.transition(1,S.CANCELLED,application_folder_relpath='one',codex_application_session_id='owner')
        restarted=ApplicationQueue(self.db);restarted.initialize()
        self.assertEqual(restarted.get(1).state,S.CANCELLED)
        self.assertFalse(restarted.enqueue(1))
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT state,application_folder_relpath,codex_application_session_id FROM application_queue WHERE job_id=1').fetchone(),('Cancelled','one','owner'))

class JobfeedSchemaCompatibilityTest(unittest.TestCase):
    setUp=QueueFixture.setUp
    def test_legacy_private_terminal_table_migrates_without_jobfeed_schema_extension(self):
        with sqlite3.connect(self.db) as c:
            c.executescript("CREATE TABLE application_terminal(job_id INTEGER PRIMARY KEY,state TEXT,application_folder_relpath TEXT,codex_application_session_id TEXT); INSERT INTO application_terminal VALUES(1,'Cancelled','one','owner');")
        self.queue.initialize()
        self.assertFalse(self.queue.enqueue(1))
        self.assertEqual(self.queue.get(1).state,S.CANCELLED)
        with sqlite3.connect(self.db) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='application_terminal'").fetchone())
