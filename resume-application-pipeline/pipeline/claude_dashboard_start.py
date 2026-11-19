"""Start Claude Appliers for a Dashboard selection.

The Dashboard request row stays the only record. Its `scope` holds the manifest once written, and the launch
record next to the manifest marks the worker as started. Status is read from the jobs' queue states and from
whether that worker is still alive.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Mapping

from pipeline.applier_discord import DEFAULT_ENV
from pipeline.applier_runtime import ApplierRuntime
from pipeline.applier_supervisor import DONE_STEPS
from pipeline.local_state import confined, now, private_json, read_only
from pipeline.config import PipelineConfig
from pipeline.dashboard_queue import SETTLED, finished
from pipeline.lifecycle import TERMINAL_STATES

WORKER_LOCKS = ('worker-supervisor.lock', 'worker-progress.lock')
HANDOVER_WAIT = 60
BUSY = 'Another application worker is running. Start these jobs after it stops.'
APPLIED = 'Every job in this start is submitted and recorded as applied.'
REVIEW = 'Confirm Submit for each draft in Application runs when you are ready.'
STOPPED = ('The application worker stopped before these jobs finished. Their drafts and Appliers are kept. '
           'Start the worker again from the cutover checklist.')


def worker_alive(pid, manifest):
    try:
        return str(manifest).encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False


@dataclass(frozen=True)
class Host:
    spawn: Callable = subprocess.Popen
    alive: Callable = worker_alive
    python: str = sys.executable
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    discord_env: Path = DEFAULT_ENV
    claude_config: Path = Path('~/.claude.json')
    interrupt: Callable = lambda pid: os.kill(pid, signal.SIGINT)
    sleep: Callable = time.sleep

    def worker_env(self):
        from pipeline.claude_applier import clean_env
        path = [str(Path(self.python).parent), *filter(None, [self.env.get('PATH')])]
        return {**clean_env(self.env), 'PATH': os.pathsep.join(path)}


def paths(root, request_id):
    manifest = confined(root, root / '.pipeline/batches' / f'dashboard-claude-{request_id}.json')
    return manifest, manifest.with_name(manifest.stem + '-launch.json')


def claude_trusts(path, config):
    """Whether `claude --bg` will start in the folder: the nearest folder with a recorded trust decision decides."""
    projects = json.loads(Path(config).expanduser().read_text()).get('projects', {})
    for folder in (Path(path).resolve(), *Path(path).resolve().parents):
        entry = projects.get(str(folder))
        if isinstance(entry, dict) and 'hasTrustDialogAccepted' in entry:
            return entry['hasTrustDialogAccepted'] is True
    return False


def missing_setup(queue, host):
    from pipeline.resume_catalog import ResumeCatalog
    root, path = queue.root, host.worker_env()['PATH']
    checks = {
        'application-profile.md': lambda: (root / 'application-profile.md').stat().st_size > 0,
        'resume-catalog.json': lambda: ResumeCatalog.load(root / 'resume-catalog.json'),
        'claude CLI': lambda: shutil.which('claude', path=path),
        'jobfeed CLI': lambda: os.access(root.parent / 'jobfeed/.venv/bin/jobfeed', os.X_OK) or shutil.which('jobfeed', path=path),
        'Discord token file': lambda: Path(host.discord_env).expanduser().is_file(),
        f'Claude trust for {root.parent} (run claude there once and accept the trust prompt)':
            lambda: claude_trusts(root / 'applications', host.claude_config),
        'worker reads this Jobfeed database': lambda: PipelineConfig.discover(root).jobfeed_db.resolve() == queue.jobfeed_db,
    }
    missing = []
    for name, check in checks.items():
        try:
            present = check()
        except (OSError, ValueError, KeyError, TypeError):
            present = False
        if not present:
            missing.append(name)
    return missing


def _launch(path):
    record = json.loads(path.read_text())
    return record['pid'], record['argv'][record['argv'].index('--batch') + 1]


def launch_records(root):
    """Launch records of the Dashboard Claude workers and of the autopilot worker."""
    batches = Path(root) / '.pipeline/batches'
    return [*batches.glob('dashboard-claude-*-launch.json'), *batches.glob('autopilot-launch.json')]


def worker_running(root, host):
    for name in WORKER_LOCKS:
        with confined(root, root / '.pipeline' / name).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    # A worker spawned a moment ago may not hold its lock yet.
    return any(host.alive(*_launch(path)) for path in launch_records(root))


def finished_workers(root, host):
    """PIDs of the running Claude workers, if every job in their batches already has a started Applier.

    Such a worker only supervises, and the supervisor and Discord bridge of any Claude worker cover every Applier,
    so a new worker can take over. None means some running worker still has jobs to start. The autopilot worker can
    always be taken over: a job it has not started stays in its continuous batch for its next run.
    """
    from pipeline.claude_applier import ClaudeApplierLauncher, never_started
    launcher, pids = ClaudeApplierLauncher(root), []
    for path in launch_records(root):
        pid, batch = _launch(path)
        if not host.alive(pid, batch):
            continue
        batch = json.loads(Path(batch).read_text())
        for candidate in [] if batch.get('continuous') else batch['ordered_candidates']:
            launch_id = launcher.launch_for(candidate['id'])
            if launch_id is None or never_started(launcher._record(launch_id)):
                return None
        pids.append(pid)
    return pids


def hand_over(root, host):
    """Stop finished Dashboard workers with one SIGINT each so a new one can start. Their Appliers keep running."""
    pids = finished_workers(root, host)
    if not pids:
        return False
    for pid in pids:
        host.interrupt(pid)
    for _ in range(HANDOVER_WAIT):
        if not worker_running(root, host):
            return True
        host.sleep(1)
    return False


def claimed(jobfeed_db, jobs):
    with closing(read_only(jobfeed_db)) as db:
        for job in jobs:
            row = db.execute('SELECT state,codex_application_session_id FROM application_queue WHERE job_id=?', (job['source_job_id'],)).fetchone()
            if not row or row['state'] != 'Queued' or row['codex_application_session_id']:
                yield job


def manifest(root, request_id, jobs, dashboard_url):
    from pipeline.resume_catalog import ResumeCatalog
    return dict(resume_mode='selected_existing',
                resume_catalog=ResumeCatalog.load(root / 'resume-catalog.json').freeze(root),
                ordered_candidates=[{'id': job['source_job_id'], **{key: job[key] for key in ('real_job_id', 'company', 'title', 'url', 'jd_text')}} for job in jobs],
                max_parallel=min(4, len(jobs)), target_successful_submissions=len(jobs), submit_authorized=False,
                direct_application_mode=True, preserve_unsubmitted_pages=True, dashboard_request_id=request_id,
                dashboard_url=dashboard_url)


def start(queue, row, host=None):
    host = host or Host()
    root, request_id = queue.root, row['request_id']
    manifest_path, launch_path = paths(root, request_id)
    if launch_path.exists():
        return
    missing = missing_setup(queue, host)
    if missing:
        queue._save(request_id, status='needs_attention', reason='Claude Appliers are not set up. Missing: ' + ', '.join(missing) + '. Nothing was started.')
        return
    if worker_running(root, host) and not hand_over(root, host):
        queue._save(request_id, status='waiting_for_capacity', reason=BUSY)
        return
    jobs = json.loads(row['jobs'])
    taken = [f"Job {job['source_job_id']} ({job['company']} · {job['title']})" for job in claimed(queue.jobfeed_db, jobs)]
    if taken:
        queue._save(request_id, status='needs_attention', reason=', '.join(taken) + ' is no longer queued without an Applier. Nothing was started.')
        return
    private_json(manifest_path, manifest(root, request_id, jobs, json.loads(row['dispatch_context'])['backend_origin']), root)
    from pipeline.orchestrator import ApplicationPipeline
    pipeline = ApplicationPipeline(PipelineConfig.discover(root))
    pipeline.initialize()
    for job in jobs:
        pipeline.enqueue(job['source_job_id'])
    output = confined(root, root / 'monitor-output' / f'dashboard-claude-{request_id}')
    output.mkdir(parents=True, exist_ok=True)
    argv = [host.python, '-m', 'pipeline.cli', '--root', str(root), 'watch', '--batch', str(manifest_path),
            '--application-workers', str(min(4, len(jobs))), '--progress-output', str(output), '--progress-local-only']
    try:
        with (output / 'worker.log').open('ab') as log:
            process = host.spawn(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, shell=False,
                                 start_new_session=True, env=host.worker_env())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        queue._save(request_id, status='needs_attention', reason='The application worker could not start: ' + str(error))
        return
    private_json(launch_path, {'argv': argv, 'pid': process.pid, 'started_at': now()}, root)
    queue._save(request_id, status='preparing', scope=str(manifest_path), reason=None)


def queue_states(jobfeed_db, jobs):
    from pipeline.jobfeed import Jobfeed
    with closing(read_only(jobfeed_db)) as db:
        predicate = Jobfeed._applied_predicate(db)
        for job in jobs:
            row = db.execute('SELECT state,codex_application_session_id FROM application_queue WHERE job_id=?', (job['source_job_id'],)).fetchone()
            applied = db.execute(f'SELECT 1 FROM jobs j WHERE j.id=? AND ({predicate})', (job['source_job_id'],)).fetchone()
            yield {'job_id': job['source_job_id'], 'queue_state': row['state'] if row else 'Already Applied' if applied else None,
                   'owner': row['codex_application_session_id'] if row else None}


def receipt(queue, row, public, host=None):
    host = host or Host()
    if row['status'] != 'preparing' or not row['scope']:
        return public
    observed = list(queue_states(queue.jobfeed_db, json.loads(row['jobs'])))
    # An Applier that recorded a finished step has settled its job while the queue still says Applying.
    done = {(job['job_id'], job['owner']) for job in ApplierRuntime(queue.root).jobs() if job['last_progress'] in DONE_STEPS}
    if all(job['queue_state'] in SETTLED | TERMINAL_STATES or (job['job_id'], job['owner']) in done for job in observed):
        settled = finished(observed)
        if settled['status'] == 'ready_for_review':
            settled['reason'] = APPLIED if all(job['queue_state'] == 'Already Applied' for job in observed) else REVIEW
        return public | settled
    manifest_path, launch_path = paths(queue.root, row['request_id'])
    alive = False
    # The autopilot worker may have taken over this start's worker. It supervises these Appliers too.
    for path in (launch_path, queue.root / '.pipeline/batches/autopilot-launch.json'):
        try:
            alive = alive or host.alive(*_launch(path))
        except (OSError, ValueError, KeyError):
            pass
    return public | ({'status': 'preparing'} if alive else {'status': 'needs_attention', 'reason': STOPPED})
