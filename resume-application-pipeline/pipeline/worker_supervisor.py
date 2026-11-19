"""Parent of the existing watch command; bounded restarts, no application actions."""
import fcntl
import json
import subprocess
import time
from pathlib import Path
from pipeline.worker_recovery import RecoveryBudget, stopped


def restart_reason(state, manifest):
    if not state or state.get('source') != str(Path(manifest).resolve()):
        return 'snapshot_unverified'
    snapshot = state.get('snapshot') or {}
    if snapshot.get('complete'):
        return 'batch_complete'
    # Active owners are not a reason to leave their controller dead: the fresh
    # worker re-reads global active turns and accounts for them before dispatch.
    eligible = {'Queued','Dispatching','Tailoring','Resume Ready','Deferred','Applying','Reviewing','Ready for Review','Submitting','Submit Authorized','Platform Wait'}
    for job in snapshot.get('jobs', []):
        if job.get('paused') or job.get('errors') or job.get('blocked') == 'submission_unknown':
            continue
        if job.get('queue_state') in eligible or ((job.get('submitted') or job.get('user_submitted')) and not job.get('closed')):
            return 'actionable_work'
    return 'no_actionable_work'


def supervise(root, manifest, output, command, *, spawn=subprocess.Popen, sleep=time.sleep):
    root, manifest, output = Path(root), Path(manifest).resolve(), Path(output)
    control = root/'.pipeline'; control.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    budget = RecoveryBudget(output/'worker-restarts.json', manifest)
    child = None
    with (control/'worker-supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'supervisor_already_running'
        while True:
            if stopped(manifest):
                return 'stopped_by_user'
            state = budget.read()
            if state['actions'].get('initial_launch'):
                if not budget.allowed('worker'):
                    return 'restart_exhausted'
                try:
                    saved = json.loads((output/'monitor.json').read_text())
                except (OSError, ValueError):
                    saved = None
                reason = restart_reason(saved, manifest)
                if reason != 'actionable_work':
                    budget.failure('restart_blocked', reason)
                    return reason
                budget.failure('worker', 'worker exited; restart same authorized batch')
            else:
                budget.failure('initial_launch', 'explicit watch launch')
            # Never create a second child while any legacy/current worker holds the lock.
            with (control/'worker-progress.lock').open('a') as worker_lock:
                try:
                    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 'worker_already_running'
            if stopped(manifest):
                return 'stopped_by_user'
            try:
                child = spawn(command, start_new_session=True)
                while child.poll() is None:
                    # The child independently checks stop before dispatch and drains.
                    # Do not kill live owners and then start another writer.
                    sleep(1)
                budget.failure('last_exit', f'worker exit {child.returncode}')
                if stopped(manifest):
                    return 'stopped_by_user'
                sleep(1)
            except KeyboardInterrupt:
                # Parent interruption never relaunches a child. Drain it to retain one writer.
                if child and child.poll() is None:
                    child.send_signal(2)
                    child.wait()
                # A drained operator stop is not a worker crash. The next explicit
                # watch launch starts a new recovery episode; no automatic relaunch.
                budget.success('initial_launch')
                budget.success('worker')
                return 'interrupted'
            except OSError as exc:
                budget.failure('last_exit', f'spawn failed: {exc}')
