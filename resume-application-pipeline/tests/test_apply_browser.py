import unittest

from pipeline.apply_browser import EXTENSION, ExtensionGate, extension_running
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


if __name__ == '__main__':
    unittest.main()
