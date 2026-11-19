import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


class RecoveryAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.manifest = self.root/'batch.json'
        self.manifest.write_text('{}'); self.output = self.root/'output'; self.output.mkdir()

    def test_restart_budget_survives_supervisor_restart(self):
        from pipeline.worker_recovery import RecoveryBudget
        for _ in range(3):
            budget = RecoveryBudget(self.output/'budget.json', 'batch')
            self.assertTrue(budget.allowed('worker'))
            budget.failure('worker', 'worker exit 1')
        budget = RecoveryBudget(self.output/'budget.json', 'batch')
        self.assertFalse(budget.allowed('worker'))
        self.assertEqual(budget.read()['actions']['worker']['reason'], 'worker exit 1')

    def test_worker_exit_restarts_only_to_bound_then_stops(self):
        from pipeline.worker_supervisor import supervise
        snapshot = {'source':str(self.manifest),'snapshot':{'counts':{'occupied':0},'complete':False,'jobs':[{'queue_state':'Resume Ready','paused':False,'errors':[]}]}}
        (self.output/'monitor.json').write_text(json.dumps(snapshot))
        child = Mock(); child.poll.return_value = 1
        spawn=Mock(return_value=child)
        result=supervise(self.root,self.manifest,self.output,['fake-worker'],spawn=spawn,sleep=lambda _:None)
        self.assertEqual(result,'restart_exhausted')
        self.assertEqual(spawn.call_count,4)
        spawn.reset_mock()
        self.assertEqual(supervise(self.root,self.manifest,self.output,['fake-worker'],spawn=spawn,sleep=lambda _:None),'restart_exhausted')
        spawn.assert_not_called()

    def test_repeated_drained_maintenance_does_not_exhaust_crash_budget(self):
        from pipeline.worker_supervisor import supervise
        from pipeline.worker_recovery import RecoveryBudget
        for _ in range(5):
            child=Mock();child.poll.return_value=None
            def interrupt(_): raise KeyboardInterrupt()
            self.assertEqual(supervise(self.root,self.manifest,self.output,['fake'],
                spawn=Mock(return_value=child),sleep=interrupt),'interrupted')
            child.send_signal.assert_called_once_with(2)
            child.wait.assert_called_once()
            self.assertNotIn('initial_launch',RecoveryBudget(self.output/'worker-restarts.json',self.manifest).read()['actions'])

    def test_user_stop_never_starts_worker(self):
        from pipeline.worker_supervisor import supervise
        self.manifest.with_name('batch-handoff.json').write_text(json.dumps({'manifest':str(self.manifest),'status':'stopped_by_user'}))
        spawn=Mock()
        self.assertEqual(supervise(self.root,self.manifest,self.output,['fake-worker'],spawn=spawn,sleep=lambda _:None),'stopped_by_user')
        spawn.assert_not_called()

    def test_waiting_alone_does_not_restart(self):
        from pipeline.worker_supervisor import restart_reason
        state={'source':str(self.manifest),'snapshot':{'complete':False,'counts':{'occupied':0},'jobs':[{'queue_state':'Needs Input','paused':False,'errors':[]}]}}
        self.assertEqual(restart_reason(state,self.manifest),'no_actionable_work')
        state['snapshot']['jobs'].append({'queue_state':'Resume Ready','paused':False,'errors':[]})
        self.assertEqual(restart_reason(state,self.manifest),'actionable_work')

class WorkerRecoveryAcceptance(unittest.TestCase):
    def test_stop_checked_before_tick_initialization(self):
        from pipeline.worker import OrchestratorWorker, WorkerSettings
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        path = root/'batch.json'; path.write_text('{}')
        path.with_name('batch-handoff.json').write_text(json.dumps({'manifest':str(path),'status':'stopped_by_user'}))
        pipeline = Mock(); pipeline.config.resume_root = root; pipeline.manifest_path = path
        worker = OrchestratorWorker(pipeline, WorkerSettings(application_workers=1, recovery_interval=0), emit=lambda _: None)
        self.addCleanup(worker.close)
        worker.tick()
        pipeline.initialize.assert_not_called()
        self.assertTrue(worker._progress_done)
        self.assertFalse(worker._application_futures)

class SupervisorIntegration(unittest.TestCase):
    setUp = RecoveryAcceptance.setUp

    def test_terminal_and_parent_interrupts_drain_child_once(self):
        import os
        import signal
        import subprocess
        import sys
        import time

        for interrupt_group in (True, False):
            with self.subTest(interrupt_group=interrupt_group):
                root = self.root / ('group' if interrupt_group else 'parent')
                root.mkdir()
                (root / 'batch.json').write_text('{}')
                (root / 'child.py').write_text('''import os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[1])
def interrupt(*_):
    (root/'signal').write_text('received')
    signal.signal(signal.SIGINT,signal.default_int_handler)
    try:
        time.sleep(.25)
        (root/'drained').write_text('completed')
    except KeyboardInterrupt:
        (root/'interrupted-drain').write_text('second signal')
    raise SystemExit(0)
signal.signal(signal.SIGINT,interrupt)
(root/'child-pid').write_text(str(os.getpid()))
while True: time.sleep(1)
''')
                (root / 'parent.py').write_text('''import sys,time
from pathlib import Path
from pipeline.worker_supervisor import supervise
root=Path(sys.argv[1])
def sleep(seconds):
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        time.sleep(.1)
        raise
print(supervise(root,root/'batch.json',root/'output',
    [sys.executable,str(root/'child.py'),str(root)],sleep=sleep),flush=True)
''')
                env = dict(os.environ)
                env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
                parent = subprocess.Popen(
                    [sys.executable, str(root / 'parent.py'), str(root)],
                    env=env, start_new_session=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                child_pid = None
                try:
                    deadline = time.monotonic() + 10
                    while not (root / 'child-pid').exists():
                        if parent.poll() is not None or time.monotonic() >= deadline:
                            self.fail('Supervisor fixture did not start its child')
                        time.sleep(.01)
                    child_pid = int((root / 'child-pid').read_text())
                    send = os.killpg if interrupt_group else os.kill
                    send(parent.pid, signal.SIGINT)
                    stdout, stderr = parent.communicate(timeout=5)
                    self.assertEqual((parent.returncode, stdout.strip(), stderr),
                                     (0, 'interrupted', ''))
                    self.assertEqual((root / 'drained').read_text(), 'completed')
                    self.assertFalse((root / 'interrupted-drain').exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                    budget = json.loads((root / 'output/worker-restarts.json').read_text())
                    self.assertEqual(budget['actions'], {})
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.communicate(timeout=5)
                    if child_pid is not None:
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_existing_worker_lock_prevents_second_process(self):
        import fcntl
        from pipeline.worker_supervisor import supervise
        control=self.root/'.pipeline'; control.mkdir()
        with (control/'worker-progress.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            spawn=Mock()
            self.assertEqual(supervise(self.root,self.manifest,self.output,['fake'],spawn=spawn,sleep=lambda _:None),'worker_already_running')
            spawn.assert_not_called()

    def test_stop_after_child_exit_prevents_restart(self):
        from pipeline.worker_supervisor import supervise
        child=Mock()
        def exited():
            self.manifest.with_name('batch-handoff.json').write_text(json.dumps({'manifest':str(self.manifest),'controller_status':'stopped_by_user'}))
            return 1
        child.poll.side_effect=exited
        spawn=Mock(return_value=child)
        self.assertEqual(supervise(self.root,self.manifest,self.output,['fake'],spawn=spawn,sleep=lambda _:None),'stopped_by_user')
        spawn.assert_called_once()

    def test_existing_watch_command_routes_to_supervisor(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from pipeline.cli import main
        args=['cli','watch','--batch',str(self.manifest),'--progress-output',str(self.output),'--progress-local-only']
        with patch('sys.argv',args), patch('pipeline.cli.PipelineConfig.discover',return_value=SimpleNamespace(resume_root=self.root)), patch('pipeline.worker_supervisor.supervise',return_value='no_actionable_work') as supervisor:
            self.assertEqual(main(),0)
        self.assertEqual(supervisor.call_args.args[-1][-1],'--worker-child')
        self.assertIn(str(self.manifest),supervisor.call_args.args[-1])
