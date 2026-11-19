import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_progress_monitor import Fixture, NOW, OWNERS, stop_with_blocker
from pipeline.progress_monitor import ProgressMonitor
from pipeline.worker import OrchestratorWorker

class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.f=Fixture(Path(self.temp.name))
    def test_explicit_controller_is_registered_at_creation(self):
        m=ProgressMonitor(self.f.sources,Path(self.temp.name)/'other-output',controller_id='controller')
        stop_with_blocker(self.f);m.tick(force=True,now=NOW)
        self.assertTrue(m.ledger.next(NOW))
    def test_database_unavailable_leaves_worker_without_snapshot(self):
        with patch('pipeline.progress_monitor.BlockerLedger',side_effect=sqlite3.OperationalError('disk unavailable')):
            m=ProgressMonitor(self.f.sources,Path(self.temp.name)/'fault-output',controller_id='controller')
            self.assertIsNone(m.ledger)
            w=OrchestratorWorker(SimpleNamespace(),progress_monitor=m,emit=lambda _:None)
            w._check_progress();w._check_progress()
            self.assertIsNone(w._progress_snapshot)
    def test_unknown_user_request_does_not_send_again_after_restart(self):
        q=self.f.monitor.ledger
        q.observe_one(job=1,owner=OWNERS[1],features={'field':'human'},evidence='captcha',now=NOW)
        t=q.next(NOW)
        request=dict(action='Solve CAPTCHA',evidence='native page',checked_sources=['profile'],why_unavailable='human validation',semantic_request='captcha',applicability='Acme')
        q.decide(t['id'],t['claim_token'],1,'user',{'review':'current page'},NOW,request)
        calls=[]
        def send(*a):calls.append(a);raise TimeoutError()
        q.deliver_requests(send,NOW)
        restart=ProgressMonitor(self.f.sources,self.f.monitor.output)
        restart.ledger.deliver_requests(send,NOW)
        self.assertEqual(len(calls),1)
        self.assertEqual(restart.ledger.board()['requests'][0]['state'],'unknown')
    def test_manifest_removal_cancels_old_issue(self):
        import json
        stop_with_blocker(self.f);self.f.monitor.tick(force=True,now=NOW)
        self.f.data['ordered_candidates']=[]
        self.f.manifest.write_text(json.dumps(self.f.data))
        self.f.monitor.tick(force=True,now=NOW)
        self.assertIsNone(self.f.monitor.ledger.next(NOW))
        with self.assertRaises(ValueError):
            self.f.monitor.ledger.observe_one(job=1,owner=OWNERS[1],features={'field':'x'},evidence='x',now=NOW)
