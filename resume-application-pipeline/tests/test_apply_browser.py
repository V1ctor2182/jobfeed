import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.applier_discord import _reply_status
from pipeline.applier_runtime import ApplierRuntime
from pipeline.apply_browser import EXTENSION, ExtensionGate, answer_outages, extension_running, reload_extension
from pipeline.batch import BatchPipeline

OFFSCREEN = {'type': 'background_page', 'url': f'chrome-extension://{EXTENSION}/offscreen.html'}
WORKER = {'type': 'service_worker', 'url': f'chrome-extension://{EXTENSION}/service-worker-loader.js'}
OPTIONS = {'type': 'page', 'url': f'chrome-extension://{EXTENSION}/options.html'}
OTHER = {'type': 'service_worker', 'url': 'chrome-extension://oclenjjohnnlhcojhbfeefpgjliadhnj/service-worker.js'}


class ExtensionRunningTests(unittest.TestCase):
    def test_the_extension_runs_while_its_offscreen_document_or_worker_does(self):
        self.assertTrue(extension_running([OFFSCREEN]))
        self.assertTrue(extension_running([WORKER]))

    def test_its_options_page_or_another_extension_does_not_count(self):
        # 2026-11-18: after earlyoom stopped the extension, only its options page and another extension's worker were left.
        self.assertFalse(extension_running([OPTIONS, OTHER]))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ExtensionGateTests(unittest.TestCase):
    def gate(self, answers):
        self.clock, self.reads = Clock(), 0

        def targets():
            self.reads += 1
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return ExtensionGate(targets=targets, clock=self.clock, every=30)

    def test_the_browser_is_read_at_most_once_per_interval(self):
        gate = self.gate([[OFFSCREEN], [OPTIONS]])
        self.assertTrue(gate.ready())
        self.clock.now = 29
        self.assertTrue(gate.ready())
        self.assertEqual(self.reads, 1)
        self.clock.now = 30
        self.assertFalse(gate.ready())

    def test_an_unreachable_browser_is_not_ready(self):
        self.assertFalse(self.gate([OSError('refused')]).ready())

    def test_each_change_is_reported_once(self):
        gate = self.gate([[OFFSCREEN], [OPTIONS], [], [WORKER]])
        for now in (0, 30, 60, 90):
            self.clock.now = now
            gate.ready()
        self.assertEqual([e['event'] for e in gate.drain()], ['apply_browser_ready', 'apply_browser_down', 'apply_browser_ready'])
        self.assertEqual(gate.drain(), [])


class Gate:
    def __init__(self, ready):
        self.value = ready

    def ready(self):
        return self.value


class BatchStartTests(unittest.TestCase):
    def batch(self, gate=None):
        batch = BatchPipeline.__new__(BatchPipeline)
        batch.autopilot_off = lambda: False
        batch.can_start_application = lambda extra=0: True
        if gate is not None:
            batch.browser_gate = gate
        return batch

    def test_no_new_applier_starts_while_the_extension_is_down(self):
        self.assertFalse(self.batch(Gate(False)).can_start_application_for(1))
        self.assertTrue(self.batch(Gate(True)).can_start_application_for(1))

    def test_a_batch_without_a_gate_starts_as_before(self):
        self.assertTrue(self.batch().can_start_application_for(1))


class FixedGate:
    def __init__(self, ready, since):
        self.value, self.ready_since = ready, since

    def ready(self):
        return self.value


class OutageAnswerTests(unittest.TestCase):
    """2026-11-18: 158 browser_disconnected blockers waited for a Done each, long after the extension ran again."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = ApplierRuntime(Path(temp.name))

    def blocker(self, job_id, at, blocker_type='browser_disconnected'):
        self.store.bind(job_id, f'owner-{job_id}', 'batch')
        request_id = self.store.ask(job_id, f'owner-{job_id}', 'blocker', f'job {job_id} has no browser', blocker_type=blocker_type)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE applier_requests SET created_at=? WHERE id=?', (at, request_id))
        return request_id

    def test_blockers_from_before_the_extension_ran_again_are_answered_done(self):
        old = self.blocker(1, '2026-11-18T01:00:00+00:00')
        new = self.blocker(2, '2026-11-18T06:00:00+00:00')
        tab = self.blocker(3, '2026-11-18T01:00:00+00:00', 'tab_missing')
        events = answer_outages(self.store, FixedGate(True, '2026-11-18T05:30:00+00:00'))
        self.assertEqual([e['job_id'] for e in events], [1])
        self.assertEqual(self.store.latest_reply(old)['text'], 'done')
        self.assertEqual(self.store.latest_reply(old)['source_kind'], 'apply_browser')
        self.assertEqual([self.store.request(r)['state'] for r in (new, tab)], ['open', 'open'],
                         'one reported while the extension ran has another cause, and another blocker type is not ours')
        self.assertEqual(answer_outages(self.store, FixedGate(True, '2026-11-18T05:30:00+00:00')), [], 'each is answered once')

    def test_nothing_is_answered_while_the_extension_is_down(self):
        request = self.blocker(1, '2026-11-18T01:00:00+00:00')
        self.assertEqual(answer_outages(self.store, FixedGate(False, None)), [])
        self.assertEqual(self.store.request(request)['state'], 'open')

    def test_the_gate_dates_each_start_of_the_extension(self):
        answers, clock = [[OFFSCREEN], [OFFSCREEN], [], [OFFSCREEN]], Clock()
        start = datetime(2026, 11, 18, tzinfo=timezone.utc)
        gate = ExtensionGate(targets=lambda: answers.pop(0), clock=clock, wall=lambda: start + timedelta(seconds=clock.now), every=30)
        seen = []
        for now in (0, 30, 60, 90):
            clock.now = now
            gate.ready()
            seen.append(gate.ready_since)
        self.assertEqual(seen, ['2026-11-18T00:00:00+00:00', '2026-11-18T00:00:00+00:00', None, '2026-11-18T00:01:30+00:00'])

    def test_discord_says_the_worker_answered(self):
        self.assertIn('connected again', _reply_status({'intent': 'answer', 'source_kind': 'apply_browser', 'text': 'done'}))


class ExtensionsPage:
    """A browser whose chrome://extensions page needs a few polls before its API is there."""

    def __init__(self, loads_after=2, answer='ok'):
        self.calls, self.polls, self.loads_after, self.answer = [], 0, loads_after, answer

    def call(self, method, params=None, session=None):
        self.calls.append((method, (params or {}).get('url') or (params or {}).get('targetId')))
        if method == 'Target.createTarget':
            return {'targetId': 'ext'}
        if method == 'Target.attachToTarget':
            return {'sessionId': 's'}
        if method == 'Runtime.evaluate' and not params.get('awaitPromise'):
            self.polls += 1
            return {'result': {'value': self.polls > self.loads_after}}
        if method == 'Runtime.evaluate':
            assert EXTENSION in params['expression']
            return {'result': {'value': self.answer}}
        return {}


class ReloadTests(unittest.TestCase):
    def test_the_extension_is_reloaded_from_a_background_extensions_tab_that_is_closed_after(self):
        cdp = ExtensionsPage()
        self.assertEqual(reload_extension(cdp, sleep=lambda _: None), 'ok')
        self.assertEqual(cdp.calls[0], ('Target.createTarget', 'chrome://extensions/'))
        self.assertEqual(cdp.calls[-1], ('Target.closeTarget', 'ext'))
        self.assertNotIn('Page.navigate', [c[0] for c in cdp.calls], 'no form tab is touched')

    def test_a_page_that_never_loads_reloads_nothing_and_still_closes(self):
        cdp = ExtensionsPage(loads_after=99)
        self.assertEqual(reload_extension(cdp, tries=3, sleep=lambda _: None), 'the extensions page did not load')
        self.assertEqual(cdp.calls[-1], ('Target.closeTarget', 'ext'))


if __name__ == '__main__':
    unittest.main()
