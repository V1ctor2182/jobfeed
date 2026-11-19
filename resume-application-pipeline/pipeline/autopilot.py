"""Autopilot: the user's standing authorization to apply to eligible jobs without a request per batch.

Only the user turns it on, with `python -m pipeline.cli autopilot on`. Anyone may turn it off. While it is on, one
continuous Claude batch admits Results jobs whose Stage B verdict is apply or consider as Applier slots free up, and
`python -m pipeline.autopilot serve` keeps that batch's worker running. Submit still needs the user's confirmation or
auto submit mode. While it is off, nothing new is admitted or started, and Appliers already working finish as usual.

Jobs on the recruiter sheet are for the user's recruiters, so a direct application could conflict with one putting the
user forward. Autopilot neither admits nor starts them until the user marks them apply_myself. The hold is derived from
the sheet on every read and never stored, so nothing can outlive that choice, and a sheet that cannot be read holds every
job not yet started. Jobs an Applier already started are left alone.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import dataclasses
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pipeline.applier_runtime import ApplierRuntime, LIVE_REQUESTS
from pipeline.local_state import confined, now, private_json, read_only
from pipeline.config import PipelineConfig
from pipeline.jobfeed import DEFAULT_API_URL, excluded_company, inspection_url
from pipeline.lifecycle import ApplicationState as S

ACTOR = 'autopilot'
ADOPTION_REASON = "A dead worker of another batch left this job unfinished, so the autopilot batch starts it again."
STRANDED_STATES = frozenset({S.APPLYING, S.RESUME_READY})
ELIGIBLE_VERDICTS = frozenset({'apply', 'consider'})
APPLIER_SLOTS = 4
# Seconds between Jobfeed reads while a slot is free. Eligible jobs change about once a day.
QUERY_INTERVAL = 60
NOT_STARTED = frozenset({S.QUEUED, S.DISPATCHING, S.TAILORING, S.TAILORING_PAUSED, S.RESUME_READY, S.DEFERRED})
# A worker that exits with an error this many times in a row stops the service until someone starts it again.
MAX_FAILURES = 3
EXHAUSTED = 3
# One worker tick lists the batch about ten times, and the tick runs about once a second.
SHEET_READ_SECONDS = 1.0
# The scan-and-evaluate unit the worker starts when no eligible job is left. Only the autopilot service's unit sets it.
SCAN_UNIT_ENV = 'JOBFEED_AUTOPILOT_SCAN_UNIT'
# A scan and its evaluation take about an hour and spend model credits, so a dry queue starts one at most this often.
SCAN_INTERVAL = timedelta(hours=3)
# Seconds before a dry queue checks again, for example after the cycle skipped because another Jobfeed run was active.
SCAN_RETRY = 15 * 60


def manifest_path(root):
    return confined(root, Path(root) / '.pipeline/batches/autopilot.json')


def launch_path(root):
    return confined(root, Path(root) / '.pipeline/batches/autopilot-launch.json')


def enabled(root):
    return ApplierRuntime(root).autopilot()['enabled']


def write_manifest(root):
    """Create the continuous batch, or refresh its frozen resume catalog. Members carry over between runs."""
    from pipeline.resume_catalog import ResumeCatalog
    path = manifest_path(root)
    data = json.loads(path.read_text()) if path.exists() else dict(
        resume_mode='selected_existing', continuous=True, ordered_candidates=[],
        max_parallel=APPLIER_SLOTS, submit_authorized=False, direct_application_mode=True,
        preserve_unsubmitted_pages=True, dashboard_url=DEFAULT_API_URL)
    data['resume_catalog'] = ResumeCatalog.load(Path(root) / 'resume-catalog.json').freeze(root)
    data['target_successful_submissions'] = len(data['ordered_candidates'])
    private_json(path, data, root)
    return path


def sheet_holds(pipeline):
    """The source jobs the recruiter sheet holds back, or None when the sheet cannot be read."""
    read_at, holds = getattr(pipeline, 'autopilot_sheet_read', (None, None))
    if read_at is not None and time.monotonic() - read_at < SHEET_READ_SECONDS:
        return holds
    emit = getattr(pipeline, 'emit', lambda event: None)
    try:
        holds = pipeline.jobfeed.recruiter_sheet_holds()
    except Exception as error:  # Whatever went wrong, this must fail closed and let the worker keep supervising.
        holds = None
        if not getattr(pipeline, 'autopilot_sheet_unavailable', False):
            pipeline.autopilot_sheet_unavailable = True
            emit({'event': 'recruiter_sheet_unavailable', 'error': str(error)[:300],
                  'next': 'admit and start nothing until the sheet can be read'})
    else:
        if getattr(pipeline, 'autopilot_sheet_unavailable', False):
            pipeline.autopilot_sheet_unavailable = False
            emit({'event': 'recruiter_sheet_available'})
    pipeline.autopilot_sheet_read = (time.monotonic(), holds)
    return holds


def startable(pipeline, items):
    """The batch's queue items without the jobs the recruiter sheet holds back, and without the jobs of a job board that
    is held until the user signs in there. A started job is always kept."""
    from pipeline import site_holds
    holds = sheet_holds(pipeline)
    waiting = [item.job_id for item in items if not item.codex_application_session_id and item.state in NOT_STARTED]
    held = site_holds.held_jobs(ApplierRuntime(pipeline.config.resume_root), pipeline.config.jobfeed_db, waiting)
    return [item for item in items if item.codex_application_session_id or item.state not in NOT_STARTED
            or (holds is not None and item.job_id not in holds and item.job_id not in held)]


def excluded(job, holds):
    """Why the continuous batch never applies to this posting, or None."""
    try:
        inspection_url(job.url)
    except ValueError:
        return 'its URL cannot be inspected'
    if excluded_company(job.company):
        return 'its company is excluded'
    return 'it is on the recruiter sheet' if job.id in holds else None


def member(job_id, real_job_id, job):
    return {'id': job_id, 'real_job_id': real_job_id, 'company': job.company, 'title': job.title, 'url': job.url, 'jd_text': job.description}


def stranded(pipeline, data):
    """The queue rows this batch may adopt from a dead worker of another batch, read without writing anything.

    A Queued row counts only if an earlier adoption requeued it and stopped before the manifest write. O(queue rows + requests).
    """
    store = ApplierRuntime(pipeline.config.resume_root)
    listed = {int(job['id']) for job in data['ordered_candidates'] + data.get('carryover_candidates', [])}
    waiting = {request['job_id'] for request in store.requests() if request['state'] in LIVE_REQUESTS}
    interrupted = {entry['job_id'] for entry in store.requeued_by(ACTOR)}
    return [item.job_id for item in pipeline.queue.list_active()
            if item.job_id not in listed and item.job_id not in waiting and not item.held and not pipeline.queue.paused(item.job_id)
            and (item.state in STRANDED_STATES or (item.state == S.QUEUED and item.job_id in interrupted))]


def adopt_stranded(pipeline):
    """Requeue the stranded jobs that `requeue` accepts and add them to the continuous batch. Returns their IDs.

    Autopilot is the user's standing authorization for this. After a reboot the only proof that an old session died is a
    service status file that the first new `claude --bg` rewrites, so the worker runs this before anything can start one,
    and it takes every eligible job whatever the free slots. Safe to repeat from any point. O(queue rows + requests) reads.
    Returns None, having done nothing, when the recruiter sheet cannot be read.
    """
    root = pipeline.config.resume_root
    if not enabled(root) or time.monotonic() < getattr(pipeline, 'autopilot_next_adoption', 0):
        return []
    holds = sheet_holds(pipeline)
    if holds is None:
        return None
    from pipeline.claude_dispatch import launcher_for
    from pipeline.requeue import requeue
    data = json.loads(pipeline.manifest_path.read_text())
    canonical = {job.get('real_job_id') for job in data['ordered_candidates']} - {None}
    reported = vars(pipeline).setdefault('autopilot_reported', set())
    emit = getattr(pipeline, 'emit', lambda event: None)

    def refuse(job_id, reason):
        if (job_id, reason) not in reported:
            reported.add((job_id, reason))
            emit({'event': 'stranded_job_refused', 'job_id': job_id, 'reason': reason})

    snapshots = {}
    for job_id in stranded(pipeline, data):
        job = pipeline.jobfeed.dispatchable_job(job_id)
        if job is None:
            refuse(job_id, 'its posting is closed or incomplete')
            continue
        with closing(read_only(pipeline.config.jobfeed_db)) as db:
            real_job_id = db.execute('SELECT real_job_id FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        reason = (excluded(job, holds) or ('its posting was ignored' if pipeline.jobfeed.is_ignored(job_id) else None)
                  or ('another source of its posting is in this batch' if real_job_id in canonical else None))
        if reason:
            refuse(job_id, reason)
            continue
        snapshots[job_id] = member(job_id, real_job_id, job)
        canonical |= {real_job_id} - {None}
    results = requeue(pipeline.config, list(snapshots), ADOPTION_REASON, worker_busy=lambda _: False, inventory=launcher_for(pipeline).agents,
                      actor=ACTOR, keep_batches=False) if snapshots else []
    adopted = [result['job_id'] for result in results if result['status'] in {'requeued', 'already_queued'}]
    for result in results:
        if result['job_id'] not in adopted:
            refuse(result['job_id'], result['reason'])
    if adopted:
        data['ordered_candidates'] += [snapshots[job_id] for job_id in adopted]
        data['target_successful_submissions'] = len(data['ordered_candidates'])
        private_json(pipeline.manifest_path, data, root)
        for job_id in adopted:
            emit({'event': 'stranded_job_adopted', 'job_id': job_id})
    # Set last: a pass that failed must run again before the worker lets anything start.
    pipeline.autopilot_next_adoption = time.monotonic() + QUERY_INTERVAL
    return adopted


def refill(pipeline):
    """Adopt stranded jobs, then admit eligible jobs into the continuous batch while autopilot is on and an Applier slot is free.

    Returns the adopted and admitted job IDs. The worker reloads the edited manifest and starts them.
    """
    root = pipeline.config.resume_root
    if not enabled(root):
        return []
    adopted = adopt_stranded(pipeline) or []
    holds = sheet_holds(pipeline)
    if holds is None:
        return adopted
    data = json.loads(pipeline.manifest_path.read_text())
    members = {int(job['id']) for job in data['ordered_candidates']}
    items = pipeline.batch_items()
    # An admitted or adopted job that has not started yet keeps its slot, also before the worker has reloaded the manifest.
    pending = sum(item.state in NOT_STARTED and not item.held for item in items) + len(members - set(pipeline.batch_ids))
    from pipeline.claude_dispatch import active_claude_owners, waiting_claude_owners
    owners = {item.codex_application_session_id for item in items if item.codex_application_session_id}
    busy = active_claude_owners(pipeline, owners) - waiting_claude_owners(pipeline, owners) if owners else set()
    free = pipeline.batch_limit - len(busy) - pending
    if free <= 0 or time.monotonic() < getattr(pipeline, 'autopilot_next_query', 0):
        return adopted
    pipeline.autopilot_next_query = time.monotonic() + QUERY_INTERVAL
    canonical = {job.get('real_job_id') for job in data['ordered_candidates']} - {None}
    # Jobs that cannot be admitted now must not fill the window: CAPTCHA holds alone once hid every admissible job.
    held = {item.job_id for item in pipeline.queue.list_active() if item.held}
    from pipeline import site_holds
    store = ApplierRuntime(root)
    site_held = lambda job_id: bool(site_holds.held_jobs(store, pipeline.config.jobfeed_db, [job_id]))  # noqa: E731
    added = []
    for job in pipeline.jobfeed.priority_jobs(limit=min(100, free + 20), include_queued=True, verdicts=ELIGIBLE_VERDICTS,
                                              skip=lambda job: job.id in members or job.id in held or excluded(job, holds) is not None or site_held(job.id)):
        if len(added) >= free:
            break
        admission = pipeline.queue.admit(job.id)
        queue_id = admission.queue_job_id
        if (admission.status not in {'queued', 'already_queued'} or admission.queue_state != S.QUEUED.value
                or queue_id is None or queue_id in members or admission.real_job_id in canonical):
            continue
        item = pipeline.queue.get(queue_id)
        queued = job if queue_id == job.id else pipeline.jobfeed.dispatchable_job(queue_id)
        if item is None or item.held or item.codex_application_session_id or queued is None:
            continue
        data['ordered_candidates'].append(member(queue_id, admission.real_job_id, queued))
        members.add(queue_id)
        canonical |= {admission.real_job_id} - {None}
        added.append(queue_id)
    if added:
        data['target_successful_submissions'] = len(data['ordered_candidates'])
        private_json(pipeline.manifest_path, data, root)
    elif not adopted:
        scan_when_dry(pipeline)
    return adopted + added


def last_scan(jobfeed_db):
    """When Jobfeed's newest scan of every source started, or None if none ever did."""
    with closing(read_only(jobfeed_db)) as db:
        started = db.execute("SELECT max(started_at) FROM pipeline_runs WHERE source='all'").fetchone()[0]
    return datetime.fromisoformat(started.replace('Z', '+00:00')) if started else None


def start_unit(unit):
    """Ask systemd to start a user unit without waiting for it to finish. Returns an error, or None."""
    try:
        result = subprocess.run(['systemctl', '--user', 'start', '--no-block', unit], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    return (result.stderr.strip() or f'exit {result.returncode}') if result.returncode else None


def scan_when_dry(pipeline):
    """A free slot found no eligible job: start a scan unless one started within SCAN_INTERVAL. Returns whether it did.

    The cycle unit checks autopilot itself, never runs twice at once and skips while another Jobfeed run is active. The
    apply and consider jobs it finds are admitted by a later refill.
    """
    unit = os.environ.get(SCAN_UNIT_ENV)
    if not unit or time.monotonic() < getattr(pipeline, 'autopilot_next_scan', 0):
        return False
    pipeline.autopilot_next_scan = time.monotonic() + SCAN_RETRY
    emit = getattr(pipeline, 'emit', lambda event: None)
    try:
        last = last_scan(pipeline.config.jobfeed_db)
    except sqlite3.Error as error:
        emit({'event': 'autopilot_scan_failed', 'error': str(error)[:300]})
        return False
    if last is not None and datetime.now(timezone.utc) - last < SCAN_INTERVAL:
        return False
    error = start_unit(unit)
    emit({'event': 'autopilot_scan_failed', 'error': error[:300]} if error else
         {'event': 'autopilot_scan_started', 'unit': unit, 'last_scan': last.isoformat() if last else None})
    return error is None


def finished(snapshot):
    """Whether a continuous batch can stop while autopilot is off: no started job still needs its worker.

    A job that never started keeps its place in the batch for the next run.
    """
    return all(job.get('terminal') or (job.get('submitted') and job.get('synced')) or not job.get('owner')
               for job in snapshot['jobs'])


def worker_command(root, python):
    root = Path(root)
    return [python, '-m', 'pipeline.cli', '--root', str(root), 'watch', '--batch', str(manifest_path(root)),
            '--application-workers', str(APPLIER_SLOTS), '--progress-output', str(root / 'monitor-output/autopilot'),
            '--progress-local-only', '--worker-child']


class Service:
    """Keeps the continuous batch's worker running while autopilot is on. One worker at a time, as everywhere."""

    def __init__(self, root, host=None, *, poll=60, log=lambda line: print(line, flush=True)):
        from pipeline.claude_dashboard_start import Host
        self.config = PipelineConfig.discover(root)
        self.root = self.config.resume_root
        host = host or Host()
        interrupted = set()

        def interrupt_once(pid):
            # A second SIGINT would cut short the drain of a worker already stopping.
            if pid not in interrupted:
                interrupted.add(pid)
                host.interrupt(pid)
        self.host = dataclasses.replace(host, interrupt=interrupt_once)
        self.poll, self.log = poll, log
        self.stopping = threading.Event()
        self.child = None

    def stop(self, *_):
        """Drain the running worker with one SIGINT, as for any worker, and stop the service after it exits."""
        if not self.stopping.is_set() and self.child is not None and self.child.poll() is None:
            self.child.send_signal(signal.SIGINT)
        self.stopping.set()

    def step(self):
        """Start the worker if autopilot is on and no other worker runs, and wait for it. Returns what happened."""
        from pipeline.claude_dashboard_start import hand_over, missing_setup, worker_running
        if not enabled(self.root):
            return 'off'
        if worker_running(self.root, self.host) and not hand_over(self.root, self.host):
            return 'another_worker'
        missing = missing_setup(SimpleNamespace(root=self.root, jobfeed_db=self.config.jobfeed_db.resolve()), self.host)
        if missing:
            return 'missing_setup: ' + ', '.join(missing)
        write_manifest(self.root)
        argv = worker_command(self.root, self.host.python)
        output = Path(argv[argv.index('--progress-output') + 1])
        output.mkdir(parents=True, exist_ok=True)
        with (self.root / '.pipeline/worker-supervisor.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'another_worker'
            with (output / 'worker.log').open('ab') as stream:
                self.child = self.host.spawn(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                             shell=False, start_new_session=True, env=self.host.worker_env())
            private_json(launch_path(self.root), {'argv': argv, 'pid': self.child.pid, 'started_at': now()}, self.root)
            if self.stopping.is_set():
                self.child.send_signal(signal.SIGINT)
            code = self.child.wait()
        self.child = None
        return f'worker_exit {code}'

    def serve(self):
        failures = 0
        while not self.stopping.is_set():
            outcome = self.step()
            self.log(json.dumps({'event': 'autopilot', 'outcome': outcome, 'at': now()}))
            if outcome.startswith('worker_exit'):
                code = int(outcome.split()[1])
                failures = 0 if code in (0, -signal.SIGINT) else failures + 1
                if failures >= MAX_FAILURES:
                    return EXHAUSTED
            self.stopping.wait(self.poll)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=None)
    parser.add_argument('command', choices=('serve', 'enabled'),
                        help='serve: keep the continuous worker running while autopilot is on. '
                             'enabled: exit 0 when autopilot is on, 1 when off')
    args = parser.parse_args(argv)
    root = PipelineConfig.discover(args.root).resume_root
    if args.command == 'enabled':
        return 0 if enabled(root) else 1
    service = Service(root)
    signal.signal(signal.SIGTERM, service.stop)
    signal.signal(signal.SIGINT, service.stop)
    return service.serve()


if __name__ == '__main__':
    raise SystemExit(main())
