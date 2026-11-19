"""Synthetic queue boundary tests; never launch applications or processes."""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime_fixture import RuntimeFixture
from pipeline.local_state import now
from pipeline.dashboard_queue import DashboardQueue


class DashboardQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = RuntimeFixture(self.temp.name)
        self.queue = DashboardQueue(self.f.root, self.f.db)

    def payload(self, ids=(1,), request='request-one'):
        return dict(request_id=request, items=[dict(real_job_id=str(1000+i), source_job_id=i) for i in ids],
                    provenance=dict(kind='local_dashboard', origin='http://localhost:7654', received_at=now()))

    def dispatch_payload(self, ids=(1,), request='request-one'):
        return self.payload(ids, request) | dict(authorize_preparation=True, backend_origin='http://127.0.0.1:7654', project_root=str(self.f.root))

    def request(self, request='request-one'):
        return self.queue.status(request)['requests'][0]

    def test_queue_and_status_are_inert(self):
        self.assertEqual(self.queue.status()['consumer']['state'], 'idle')
        self.assertFalse(self.queue.path.exists())
        row = self.queue.enqueue(self.payload())
        self.assertEqual((row['intent'], row['status'], row['plan_id']), ('queue', 'queued', None))
        before = self.queue.path.read_bytes()
        for _ in range(3):
            self.assertEqual(self.queue.status()['consumer']['state'], 'idle')
        self.assertEqual(self.queue.path.read_bytes(), before)
        self.assertFalse((self.f.root / '.pipeline/batches').exists())
        self.assertEqual(self.queue.enqueue(self.payload()), row)
        with self.assertRaisesRegex(ValueError, 'explicit controller dispatch'):
            self.queue.handoff(self.payload())
        with self.assertRaisesRegex(ValueError, 'another selection'):
            self.queue.handoff(self.dispatch_payload())
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            self.queue.enqueue(self.dispatch_payload((2,), 'other'))

    def test_legacy_request_is_inert_and_cannot_be_promoted(self):
        from pipeline.dashboard_queue import SCHEMA
        from pipeline.local_state import dump
        self.queue.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.payload()
        with sqlite3.connect(self.queue.path) as db:
            db.execute(SCHEMA)
            db.execute('INSERT INTO preparation_requests VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)',
                       ('request-one', dump(payload['items']), dump(payload['provenance']), '[]', '[]', 'queued', now()))
        before = self.queue.path.read_bytes()
        self.assertEqual(self.request()['intent'], 'legacy_automatic')
        self.assertEqual(self.queue.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'another selection'):
            self.queue.handoff(self.dispatch_payload())
        self.assertFalse((self.f.root / '.pipeline/batches').exists())

    def test_bounds_nullable_rejections_and_duplicate_source_outcomes(self):
        with self.assertRaisesRegex(ValueError, '1 to 100'):
            self.queue.enqueue(self.payload(range(1, 102)))
        payload = self.payload()
        payload['rejected_items'] = [dict(real_job_id=None, source_job_id=500, reason='Missing source'),
                                     dict(real_job_id='1001', source_job_id=501, reason='Duplicate canonical job')]
        row = self.queue.enqueue(payload)
        self.assertEqual([item['status'] for item in row['items']], ['rejected', 'rejected', 'queued'])
        self.assertEqual(self.queue.enqueue(payload), row)

    def test_interrupted_queue_admission_only_recovers_on_explicit_replay(self):
        with patch.object(self.queue, '_admit', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):
                self.queue.enqueue(self.payload())
        self.assertEqual(self.request()['status'], 'unknown_delivery')
        self.assertEqual(self.request()['items'][0]['real_job_id'], '1001')
        self.queue.status()
        self.assertEqual(self.queue.enqueue(self.payload())['status'], 'queued')
        self.assertFalse((self.f.root / '.pipeline/batches').exists())


if __name__ == '__main__':
    unittest.main()
