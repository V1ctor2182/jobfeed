"""Put jobs stranded by a dead worker back in the application queue, on the user's explicit request or, while the user's
autopilot mode is on, on its standing authorization: the continuous batch's worker adopts them (pipeline.autopilot.adopt_stranded).

Only a Resume Ready or Applying job qualifies, with no submission evidence anywhere, while no worker runs and its
Claude Applier session has ended. A Submit attempt the Applier recorded as sending nothing is not such evidence, nor is an
approval request_submit never spent, since request_submit spends one before any click. Those approvals move into the
requeue record and their Dashboard requests become not submitted, so the new run starts unapproved. An attempt recorded as
unknown is evidence, unless the user states for that job that it sent nothing (`unknown_sent_nothing`): the form stopped on an
emailed code nobody entered, say. An Applying job's Applier is retired first: its records move into the requeue
record, its questions close, and its job folder stays. Every batch that lists the job stops claiming it unless the caller keeps it there. Nothing
touches the employer site, and a cancelled or finished job is never reopened.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from pipeline.applier_runtime import ATTEMPTED, RETRYABLE_SUBMIT, RUN_TABLES, SUBMIT_TABLES, ApplierRuntime
from pipeline.claude_applier import CLI_STATES, ENDED, ClaudeApplierLauncher, InventoryUnavailable
from pipeline.local_state import private_json, read_only
from pipeline.claude_dispatch import is_claude_owner
from pipeline.dashboard_submission import OPEN, DashboardSubmissions
from pipeline.jobfeed import Jobfeed
from pipeline.lifecycle import ApplicationState
from pipeline.resume_catalog import retire_selection
from pipeline.store import ApplicationQueue
from pipeline.submission_reservation import admission_lock

ACTOR = 'user via pipeline.cli requeue'
SUBMISSION_FILES = ('submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json',
                    'submission-attempt.json', 'submission-result.json')
# A Dashboard Submit request in one of these states ended its attempt; the second set says nothing about whether the
# approval was spent, so such a request counts only as the record of an approval the Applier consumed or never spent.
UNSENT_REQUESTS, ENDED_REQUESTS = frozenset({'not_submitted'}), frozenset({'blocked', 'turn_ended'})
DAEMON_STATUS = Path.home() / '.claude/daemon.status.json'


@dataclass(frozen=True)
class SubmitHistory:
    """A job's Submit records judged together: none, approvals the Applier never spent, one settled attempt that sent
    nothing, or blocking evidence.

    The requeue retires them: every approval in `approvals`, used or `unspent`, the Dashboard requests in `requests`,
    marked not submitted, and the queue's copy of the approval, matched exactly when cleared."""
    blocking: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    approvals: frozenset[str] = frozenset()
    requests: tuple[str, ...] = ()
    queue_authorization: str | None = None
    unspent: frozenset[str] = frozenset()

    @property
    def kind(self):
        if self.blocking:
            return 'blocking'
        if self.unspent and self.approvals == self.unspent:
            return 'unspent'
        return 'settled_unsent' if self.settled else 'none'


@dataclass(frozen=True)
class Session:
    """Whether a launch's Claude session is 'running', 'ended' or 'unknown', and the rule that decided it."""
    state: str
    rule: str


@dataclass(frozen=True)
class Stranded:
    """What the stores say about one job, read without writing anything."""
    job_id: int
    state: str | None
    owner: str | None
    folder: str | None
    evidence: tuple[str, ...]
    submit: SubmitHistory
    applier_owner: str | None
    launch: dict | None
    batches: tuple[Path, ...]
    suspended_in: tuple[Path, ...]


def watch_process():
    """Whether any `pipeline.cli watch` process runs, supervised or not."""
    for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            argv = cmdline.read_bytes().split(b'\0')
        except OSError:
            continue
        if b'pipeline.cli' in argv and b'watch' in argv:
            return True
    return False


def any_worker(root):
    from pipeline.claude_dashboard_start import Host, worker_running
    return worker_running(root, Host()) or watch_process()


def _query(path, sql, params):
    if not path.is_file():
        return []
    with closing(read_only(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, params)]


def _approval(record):
    try:
        return json.loads(record)['source']['event_id']
    except (ValueError, KeyError, TypeError):
        return None


def submit_history(runtime, requests, queue_authorization, unknown_sent_nothing=False, retry_attempt=False):
    """Judge a job's Submit records. `runtime` holds its rows per SUBMIT_TABLES, including any this requeue already retired.

    unknown_sent_nothing is the user's statement that an attempt recorded as unknown sent nothing, so it and its still
    open Dashboard request count as settled. retry_attempt is the user's standing rule that an attempt whose outcome nobody
    saw, recorded as requested or unknown, is applied again (see ATTEMPTED)."""
    unsent = RETRYABLE_SUBMIT | ({'unknown'} if unknown_sent_nothing else set()) | (ATTEMPTED if retry_attempt else set())
    blocking = [table for table in ('applier_outbox', 'applier_skips') if runtime[table]]
    blocking += [f"applier_submits is {row['state']}" for row in runtime['applier_submits'] if row['state'] not in unsent]
    unused = sorted(row['event_id'] for row in runtime['applier_submit_auth'] if not row['used_at'])
    # request_submit spends an approval before the click, so an unused one sent nothing unless an open attempt may still spend it.
    unspent = frozenset() if blocking else frozenset(unused)
    blocking += [f'applier_submit_auth {event} is unused' for event in unused if event not in unspent]
    used = frozenset(row['event_id'] for row in runtime['applier_submit_auth'] if row['used_at'])
    settled = [f"applier_submits is {row['state']} after {row['attempts']} attempt(s)" for row in runtime['applier_submits'] if row['state'] in unsent]
    settled += [f'applier_submit_auth {event} was used' for event in sorted(used)]
    settled += [f'applier_submit_auth {event} was never spent' for event in sorted(unspent)]
    pending = []
    for request in requests:
        description = f"Dashboard Submit confirmation {request['id']} is {request['state']}"
        if request['state'] in UNSENT_REQUESTS:
            settled.append(description)
        elif ((request['state'] in ENDED_REQUESTS | (OPEN if unknown_sent_nothing or retry_attempt else set()) and request['id'] in used)
              or (request['state'] in OPEN | ENDED_REQUESTS and request['id'] in unspent)):
            settled.append(description)
            pending.append(request['id'])
        else:
            blocking.append(description)
    approvals = used | unspent
    if queue_authorization:
        (settled if _approval(queue_authorization) in approvals else blocking).append('submit authorization in the queue')
    return SubmitHistory(tuple(blocking), tuple(settled), approvals, tuple(pending), queue_authorization, unspent)


def batches_listing(root, job_id):
    for path in sorted((root / '.pipeline/batches').glob('*.json')):
        if path.stem.endswith(('-launch', '-handoff')):
            continue
        data = json.loads(path.read_text())
        if job_id in {int(c['id']) for c in data.get('ordered_candidates', []) + data.get('carryover_candidates', [])}:
            yield path


def observe(config, job_id, unknown_sent_nothing=False, retry_attempt=False):
    root, pipeline = config.resume_root, config.resume_root / '.pipeline'
    with closing(read_only(config.jobfeed_db)) as db:
        row = db.execute('SELECT * FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
        applied = db.execute(f'SELECT 1 FROM jobs j WHERE j.id=? AND ({Jobfeed._applied_predicate(db)})', (job_id,)).fetchone()
    folder = row['application_folder_relpath'] if row else None
    evidence = [name for name in SUBMISSION_FILES if folder and (config.applications_root / folder / name).exists()]
    evidence += ['applied in Jobfeed'] if applied else []
    evidence += [f'submission closeout {job_id}.json'] if (pipeline / 'submission-closeouts' / f'{job_id}.json').exists() else []
    if _query(pipeline / 'execution-gates.sqlite3', "SELECT 1 FROM execution_user_updates WHERE job_id=? AND kind='submission' "
              "UNION SELECT 1 FROM execution_cancellations WHERE job_id=? AND reason='submitted_user_confirmed'", (job_id, job_id)):
        evidence.append('user-reported submission')
    state, owner = (row['state'], row['codex_application_session_id']) if row else (None, None)
    runtime = ApplierRuntime(root)
    # A requeue that stopped after retiring the Applier already moved the attempt's rows into its record.
    entry = runtime.requeue_id(job_id, state, folder, owner)
    retired = next((r['retired'] for r in runtime.requeues(job_id) if r['id'] == entry), {})
    rows = {table: runtime._read(f'SELECT * FROM {table} WHERE job_id=?', (job_id,)) + retired.get(table, []) for table in SUBMIT_TABLES}
    requests = _query(pipeline / 'dashboard-submissions.sqlite3', 'SELECT id,state FROM requests WHERE job=? ORDER BY created_at', (job_id,))
    submit = submit_history(rows, requests, row['submit_authorization'] if row else None, unknown_sent_nothing, retry_attempt)
    applier = runtime._read('SELECT owner FROM applier_jobs WHERE job_id=?', (job_id,))
    launch = runtime._read('SELECT launch_id,name,config FROM applier_launches WHERE job_id=?', (job_id,))
    if retry_attempt and launch:
        # Under the user's retry rule a receipt Jobfeed rejects for showing no acknowledgement proves nothing was sent.
        from pipeline.receipts import RECEIPT_FILES, UNACKNOWLEDGED, verdict
        if verdict(job_id, launch[0]) == UNACKNOWLEDGED:
            evidence = [name for name in evidence if name not in RECEIPT_FILES]
    batches = tuple(batches_listing(root, job_id))
    suspended_in = tuple(path for path in batches if job_id in json.loads(path.read_text()).get('suspended_job_ids', []))
    return Stranded(job_id, state, owner, folder, tuple(evidence), submit, applier[0]['owner'] if applier else None, launch[0] if launch else None,
                    batches, suspended_in)


def process_start(pid):
    """Field 22 of /proc/<pid>/stat, the process start time in clock ticks since boot, or None if no such process runs."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return None
    return stat.rsplit(')', 1)[1].split()[19]


def boot_time():
    """The current boot's start in epoch seconds, from the btime line of /proc/stat."""
    for line in Path('/proc/stat').read_text().splitlines():
        if line.startswith('btime '):
            return int(line.split()[1])
    raise ValueError('/proc/stat has no btime line')


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds')


def stopped_service(status_path=DAEMON_STATUS, start_of=process_start, booted_at=boot_time):
    """Why the Claude background service is provably not running, or None when it may be.

    `claude agents` repeats the last state the service recorded, so after a reboot it still lists working sessions that
    nothing runs. A supervisor that is gone, or whose pid now belongs to a later process, is not proof alone: the session
    hosts it started may outlive it. The status file must also predate this boot, since a reboot ends all of them."""
    try:
        status = json.loads(Path(status_path).read_text())
        pid, started, written = int(status['supervisorPid']), str(status['supervisorProcStart']), status['writtenAt']
        if type(written) not in (int, float):
            raise TypeError('writtenAt is not a number')
        written = int(written) / 1000
        current = start_of(pid) if pid > 0 else started
        booted = booted_at()
    except (OSError, ValueError, OverflowError, KeyError, TypeError, IndexError):
        return None
    if current == started or written >= booted:
        return None
    gone = f'pid {pid} now belongs to a process started at {current}, not {started}' if current is not None else f'its supervisor pid {pid} is not running'
    return (f'the Claude background service is down: {gone}, and {status_path} was last written at {_utc(written)}, '
            f'before this boot at {_utc(booted)}')


def session(launch, agents, service_down, booted=None):
    """Judge the launch's session from the CLI inventory, and from the background service when the CLI says it runs.

    After a reboot the CLI keeps listing dead sessions as working or blocked, without a pid. One that started before this
    boot (booted, in epoch seconds) has ended, as the launcher judges it (2026-11-18: six such jobs could not be requeued)."""
    if agents is None:
        return Session('unknown', 'claude agents --json did not answer')
    cwd = json.loads(launch['config'])['cwd']

    def state(agent):
        if booted is not None and not agent.get('pid') and agent.get('startedAt', booted * 1000) < booted * 1000:
            return 'finished'
        return CLI_STATES.get(agent.get('state'), 'unknown')
    states = [state(a) for a in agents if a.get('name') == launch['name'] and a.get('cwd') == cwd]
    if all(state in ENDED for state in states):
        return Session('ended', f"claude agents reports it {', '.join(states)}" if states else 'claude agents lists no session for it')
    if service_down and all(state in ENDED | {'running'} for state in states):
        return Session('ended', f'claude agents reports it running, but {service_down}')
    return Session('running', f"claude agents reports it {', '.join(states)}")


def refusal(job, worker_busy, session):
    if job.state is None:
        return 'it is not in the application queue'
    if job.state not in {ApplicationState.RESUME_READY, ApplicationState.APPLYING}:
        return f'it is {job.state}; only Resume Ready or Applying jobs go back to the queue'
    if job.evidence or job.submit.blocking:
        return 'it has submission, skip or sync records: ' + ', '.join(job.evidence + job.submit.blocking)
    if worker_busy:
        return 'an application worker is running; stop it first'
    if job.state == ApplicationState.RESUME_READY and (job.owner or job.applier_owner or job.launch):
        return 'it is Resume Ready but has an Applier record'
    if job.state == ApplicationState.APPLYING and not is_claude_owner(job.owner):
        return 'it is owned by a Codex session; only a Claude Applier can be retired here'
    if job.applier_owner not in {None, job.owner} or (job.launch and job.launch['launch_id'] != job.owner):
        return 'its Applier records belong to another owner'
    if job.launch and session.state == 'unknown':
        return f'the Claude session inventory is unavailable, so its Applier cannot be shown to have stopped ({session.rule})'
    if job.launch and session.state == 'running':
        return f'its Claude Applier session is still running: {session.rule}'
    return None


def plan(job, keep_batches):
    """keep_in_batches names only batches that will start the job again. A suspension an earlier requeue left stays under either flag."""
    listed, suspended = [str(path) for path in job.batches], [str(path) for path in job.suspended_in]
    retire = job.owner and {'owner': job.owner, 'records_moved_into_requeue_record': list(RUN_TABLES),
                            'closed': 'open, answered and escalated questions; undelivered messages'}
    attempt = job.submit.settled and {'records': list(job.submit.settled), 'moved_into_requeue_record': ['applier_submit_auth', 'applier_submits'],
                                      'unspent_approvals_withdrawn': sorted(job.submit.unspent),
                                      'dashboard_requests_marked_not_submitted': list(job.submit.requests),
                                      'queue_submit_authorization': 'cleared' if job.submit.queue_authorization else None}
    return {'release_from_batches': [] if keep_batches else listed,
            'keep_in_batches': [path for path in listed if path not in suspended] if keep_batches else [],
            'already_suspended_in_batches': suspended, 'retire_applier': retire, 'retire_unsent_submit_attempt': attempt,
            'keep_folder': job.folder, 'queue': {'state': 'Queued', 'owner': None, 'retry_count': 0, 'next_retry_at': None,
                      'application_folder_relpath': None, 'manual_priority': 'kept'}}


def release(root, path, job_id):
    data = json.loads(path.read_text())
    if job_id not in data.get('suspended_job_ids', []):
        private_json(path, data | {'suspended_job_ids': sorted({*data.get('suspended_job_ids', []), job_id})}, root)


def requeue(config, job_ids, reason, *, dry_run=False, worker_busy=any_worker, inventory=None, service_status=DAEMON_STATUS, start_of=process_start,
            booted_at=boot_time, actor=ACTOR, keep_batches=False, unknown_sent_nothing=False, retry_attempt=False):
    """keep_batches leaves the job in every batch that lists it, so that batch starts it again once it is eligible.
    unknown_sent_nothing is the user's statement, for every listed job, that a Submit attempt recorded as unknown sent nothing."""
    root = config.resume_root
    inventory = inventory or ClaudeApplierLauncher(root).agents
    results = []
    with admission_lock(root):
        busy = worker_busy(root)
        jobs = [observe(config, job_id, unknown_sent_nothing, retry_attempt) for job_id in job_ids]
        agents, service_down, booted = None, None, None
        if any(job.launch for job in jobs):
            try:
                agents = inventory()
            except InventoryUnavailable:
                agents = None
            service_down = stopped_service(service_status, start_of, booted_at)
            try:
                booted = booted_at()
            except (OSError, ValueError):
                booted = None
        for job in jobs:
            if job.state == ApplicationState.QUEUED and job.owner is None:
                results.append({'job_id': job.job_id, 'status': 'already_queued', 'requeues': ApplierRuntime(root).requeues(job.job_id),
                                'already_suspended_in_batches': [str(path) for path in job.suspended_in]})
                continue
            judged = session(job.launch, agents, service_down, booted) if job.launch else None
            reason_refused = refusal(job, busy, judged)
            if reason_refused:
                results.append({'job_id': job.job_id, 'status': 'refused', 'state': job.state, 'reason': reason_refused})
                continue
            result = {'job_id': job.job_id, 'previous_state': job.state, 'submit_history': job.submit.kind, 'plan': plan(job, keep_batches)}
            result |= {'applier_session': judged.rule} if judged else {}
            if dry_run:
                results.append(result | {'status': 'would_requeue'})
                continue
            released = () if keep_batches else job.batches
            for path in released:
                release(root, path, job.job_id)
            entry = ApplierRuntime(root).requeue(job.job_id, job.state, job.folder, job.owner, map(str, released), actor=actor, reason=reason,
                                                 settled_auth=job.submit.approvals if job.submit.settled else None,
                                                 unknown_sent_nothing=unknown_sent_nothing, retry_attempt=retry_attempt)
            if job.folder:
                retire_selection(config.applications_root / job.folder, entry['id'])
            if job.submit.requests:
                DashboardSubmissions(config).settle_requeued(job.job_id, job.submit.requests, reason)
            if not ApplicationQueue(config.jobfeed_db).return_to_queue(job.job_id, ApplicationState(job.state), job.owner, job.submit.queue_authorization):
                results.append(result | {'status': 'refused', 'reason': 'its queue row changed while it was being requeued'})
                continue
            results.append(result | {'status': 'requeued', 'audit': {k: entry[k] for k in ('id', 'actor', 'reason', 'requeued_at', 'retired_owner')},
                                     'retired_questions': entry['retired'].get('applier_requests', [])})
    return results
