import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pipeline.store import ApplicationQueue
from pipeline.results import validate_advice_request, validate_advice_result, parse_application_result


def request(**changes):
    value = dict(luna_session_id='luna-1', revision=1, category='missing_fact', question='Which value?', evidence=['Page asks required value'], attempted=['Read profile'], suspected_critical=False)
    value.update(changes)
    return value


class AdviceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.sqlite'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO jobs VALUES(1)')
        self.queue = ApplicationQueue(self.path)
        self.queue.initialize()
        self.queue.enqueue(1)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='luna-1' WHERE job_id=1")

    def test_claim_and_restart_and_binding(self):
        with self.assertRaises(ValueError):
            self.queue.request_advice(1, request(luna_session_id='wrong'))
        item = self.queue.request_advice(1, request())
        with self.assertRaises(ValueError):
            self.queue.request_advice(1, request())
        self.assertTrue(self.queue.claim_advice(item['id']))
        self.assertFalse(self.queue.claim_advice(item['id']))
        self.queue.update_advice(item['id'], 1, advisor_thread_id='advice-1', advisor_turn_id='turn-1')
        restarted = ApplicationQueue(self.path)
        restarted.initialize()
        self.assertEqual(restarted.current_advice(1)['advisor_turn_id'], 'turn-1')
        self.assertEqual(restarted.list_advice(job_ids=[]), [])

    def test_followup_clears_old_result_and_rejects_stale_write(self):
        item = self.queue.request_advice(1, request())
        self.queue.update_advice(item['id'], 1, status='delivering', advisor_thread_id='advice-1', result={'decision':'continue','reason':'Known','instructions':'Fill it','verification':'Read back'}, delivery_turn_id='delivery-1')
        newer = self.queue.request_advice(1, request(advice_id=item['id'], revision=2))
        self.assertEqual(newer['advisor_thread_id'], 'advice-1')
        self.assertIsNone(newer['result'])
        self.assertIsNone(newer['delivery_turn_id'])
        with self.assertRaises(ValueError):
            self.queue.update_advice(item['id'], 1, status='answered')
        self.queue.update_advice(item['id'], 2, status='resolved')
        next_item = self.queue.request_advice(1, request())
        self.assertNotEqual(next_item['id'], item['id'])

    def test_critical_persists_and_does_not_cascade_with_queue(self):
        item = self.queue.request_advice(1, request(category='correctness_risk', suspected_critical=True, pause_scope='global'))
        self.assertEqual(item['status'], 'critical')
        self.assertTrue(self.queue.paused())
        self.assertTrue(self.queue.paused(99))
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM application_queue')
        self.assertIsNotNone(self.queue.get_advice(item['id']))
        self.queue.update_advice(item['id'], 1, status='resolved', pause_scope=None)
        self.assertFalse(self.queue.paused())

    def test_parallel_claim_has_one_owner_and_direct_critical_result_pauses(self):
        item = self.queue.request_advice(1, request())
        with ThreadPoolExecutor(max_workers=10) as workers:
            claims = list(workers.map(lambda _: self.queue.claim_advice(item['id']), range(10)))
        self.assertEqual(sum(claims), 1)
        self.queue.update_advice(item['id'], 1, result={'decision':'critical', 'reason':'Wrong resume', 'evidence':['Wrong graduation version'], 'pause_scope':'job'})
        self.assertTrue(self.queue.paused(1))
        self.assertFalse(self.queue.paused(2))
        self.assertFalse(self.queue.paused())

    def test_explicit_critical_resume_is_atomic_and_keeps_other_pause(self):
        item = self.queue.request_advice(1, request(category='correctness_risk', suspected_critical=True, pause_scope='global'))
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO jobs VALUES(2)')
        self.queue.enqueue(2)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='luna-2' WHERE job_id=2")
        self.queue.request_advice(2, request(luna_session_id='luna-2', category='correctness_risk', suspected_critical=True, pause_scope='global'))
        followup = request(advice_id=item['id'], revision=2)
        with self.assertRaises(ValueError):
            self.queue.request_advice(1, followup)
        updated = self.queue.request_advice(1, followup, resume_critical=True)
        self.assertEqual(updated['status'], 'pending')
        self.assertIsNone(updated['pause_scope'])
        self.assertTrue(self.queue.paused())

    def test_luna_resolve_requires_current_delivery_and_identity(self):
        item = self.queue.request_advice(1, request())
        with self.assertRaises(ValueError):
            self.queue.resolve_advice(1, item['id'], 1, 'luna-1')
        self.queue.update_advice(item['id'], 1, status='delivering', delivery_turn_id='delivery-1')
        for job, revision, session in ((2,1,'luna-1'), (1,2,'luna-1'), (1,1,'wrong')):
            with self.assertRaises(ValueError):
                self.queue.resolve_advice(job, item['id'], revision, session)
        resolved = self.queue.resolve_advice(1, item['id'], 1, 'luna-1')
        self.assertEqual(resolved['status'], 'resolved')
        self.assertIsNone(self.queue.current_advice(1))

    def test_contracts_and_chinese_status(self):
        self.assertEqual(parse_application_result('申请进度：需要主控判断\n说明：等待回答')['status'], 'needs_advice')
        for bad in (request(category='control_failure'), request(revision=True), request(suspected_critical=True), request(category='application_answer')):
            with self.assertRaises(ValueError):
                validate_advice_request(bad)
        for bad in ({'decision':'continue','reason':'x'}, {'decision':'critical','reason':'x','pause_scope':'global'}):
            with self.assertRaises(ValueError):
                validate_advice_result(bad)
        validate_advice_result({'decision':'needs_user','reason':'Missing','user_question':'Which value?'})

if __name__ == '__main__':
    unittest.main()
