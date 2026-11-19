"""Plain-code supervision of Claude Appliers: model escalation, parking idle sessions, stopping the sessions of finished jobs,
and the watchdog. It never calls a model.

The watchdog only records alerts. It does not restart, stop, or fix a session.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import NamedTuple

from pipeline.applier_runtime import RETRYABLE_SUBMIT
from pipeline.claude_applier import ENDED, PARKED, ClaudeApplierLauncher, has_process
from pipeline.submission_reservation import admission_lock

STALL_AFTER = timedelta(minutes=10)
# Longer than STALL_AFTER, so the watchdog still reports a session stuck without a question before it is parked.
PARK_AFTER = timedelta(minutes=15)
OWN_FLOW_BLOCKERS = frozenset({'captcha', 'tab_missing'})
# How long a finished job's Applier may keep working, to close its tabs and end its turn, before its session is stopped anyway.
CLOSE_GRACE = timedelta(minutes=5)
# Longer than the slowest wake, three inventory reads and one attach, which is about five minutes.
CLAIM_LOST_AFTER = timedelta(minutes=10)
LOOP_READBACKS = 3
# Each restarted Applier is a Claude session and a browser tab. Answering many at once must not start them all in one pass.
RESUME_AT_ONCE = 4
DONE_STEPS = frozenset({'ready for review', 'submitted'})
MESSAGE_UNCONFIRMED = ('A message to the Applier was being delivered when delivery stopped, so whether it arrived is unknown. '
                       'It will not be sent again. Check the Applier. The message: {text}')


def watch(store, launches, now):
    """Return (job_id, kind, detail) for every condition that warrants one message to the user."""
    requests = store.requests()
    readbacks = Counter((r['job_id'], r['field']) for r in store.readback_fields())
    findings = []
    for job in store.jobs():
        job_id, launch = job['job_id'], launches.get(job['job_id'])
        if launch is None:
            continue
        waiting = any(r['job_id'] == job_id and r['state'] in {'open', 'escalated'} for r in requests)
        last = datetime.fromisoformat(job['last_progress_at'] or launch['created_at'])
        submitted = (store.submit_state(job_id) or {}).get('state') == 'submitted' or store.skip(job_id) is not None
        # A submitted or skipped job is over, even while its session runs on to close its tabs (2026-11-12: three jobs the
        # user had just skipped were reported stalled seconds later).
        if launch['state'] == 'running' and not waiting and not submitted and now - last >= STALL_AFTER:
            findings.append((job_id, 'stalled', f"no progress since {last.isoformat()}"))
        # A stopped session cannot receive a reply, because its waiting call died with it.
        escalating = any(r['job_id'] == job_id and r['state'] == 'escalated' for r in requests)
        handing_off = launch['state'] == 'finished' and escalating
        # An Applier ends its turn on a CAPTCHA and waits, held, for the user to solve it.
        captcha = any(r['job_id'] == job_id and r['blocker_type'] == 'captcha' and r['state'] == 'open' for r in requests)
        if launch['state'] in ENDED and not handing_off and not captcha and job['last_progress'] not in DONE_STEPS and not submitted:
            asking = any(r['job_id'] == job_id and r['state'] == 'open' for r in requests)
            open_request = 'while waiting on the user' if asking else 'after'
            findings.append((job_id, 'stopped_unfinished', f"{launch['state']} {open_request}: {job['last_progress'] or 'no progress'}"))
        live = Counter(r['field'] for r in requests if r['job_id'] == job_id and r['kind'] == 'blocker' and r['state'] in {'open', 'answered'} and r['field'])
        findings += [(job_id, 'repeat_blocker', field) for field, n in live.items() if n >= 2]
        findings += [(job_id, 'readback_loop', field) for (j, field), n in readbacks.items() if j == job_id and n >= LOOP_READBACKS]
    # A change request may not be safe to apply twice, so a lost claim goes to the user instead of being released.
    findings += [(m['job_id'], 'message_unconfirmed', MESSAGE_UNCONFIRMED.format(text=m['text'][:500])) for m in store.claimed_messages()
                 if now - datetime.fromisoformat(m['claimed_at']) >= CLAIM_LOST_AFTER]
    return findings


def drain_outbox(store, handlers):
    """Retry each pending Jobfeed update: the sync of a verified submission, or a skip the user asked for.

    handlers maps an outbox kind to its callback. A failure never touches the employer submission.
    """
    events = []
    for row in store.outbox(pending=True):
        if (handler := handlers.get(row['kind'])) is None:
            continue
        try:
            handler(row['job_id'])
        except Exception as exc:  # The outbox records any sync failure and retries on the next pass.
            store.outbox_result(row['job_id'], error=exc)
            events.append({'event': 'sync_failed', 'job_id': row['job_id'], 'error': str(exc)[:200]})
        else:
            store.outbox_result(row['job_id'])
            events.append({'event': 'synced' if row['kind'] == 'jobfeed_sync' else row['kind'], 'job_id': row['job_id']})
    return events


class Park(NamedTuple):
    awaiting: str | None


def parking(launch, requests, submit, finished, now):
    """Whether to park one reconciled launch now: None to leave it alone, otherwise Park with the open request it waits on.

    requests are the job's requests, oldest first. Left alone: no live process, idle for less than PARK_AFTER, a finished
    job (stop_finished owns it), a recorded Submit attempt (the job keeps its session), an escalation waiting for the
    supervisor, or an open CAPTCHA or lost-tab blocker (their own flows stop or requeue the session).
    """
    if not has_process(launch) or launch['idle_since'] is None or now - datetime.fromisoformat(launch['idle_since']) < PARK_AFTER:
        return None
    if finished or (submit is not None and submit['state'] not in RETRYABLE_SUBMIT):
        return None
    if any(r['state'] == 'escalated' or (r['state'] == 'open' and r['blocker_type'] in OWN_FLOW_BLOCKERS) for r in requests):
        return None
    waiting = [r['id'] for r in requests if r['state'] == 'open']
    return Park(waiting[-1] if waiting else None)


def busy(launches):
    """How many Appliers are working: running and not idle. The launcher clears idle_since when it restarts a session."""
    return sum(record['state'] == 'running' and record.get('idle_since') is None for record in launches.values())


def park_idle(launcher, launches, now):
    """Restart each parked Applier whose awaited request closed, then park each Applier idle for PARK_AFTER.

    At most RESUME_AT_ONCE Appliers work at a time through restarts here. The rest stay parked and a later pass
    restarts them as the working ones finish or go idle.

    Holds the admission lock, which a Submit confirmation holds while it authorizes and wakes the Applier, so a
    confirmation never types into a session this pass is stopping. launches maps job IDs to reconciled launches; each one
    resumed or parked is replaced by its record afterwards.
    """
    store, events = launcher.store, []
    with admission_lock(launcher.root):
        requests = store.requests()
        state_of = {r['id']: r['state'] for r in requests}
        finished = {row['job_id'] for row in store.outbox()}
        slots = RESUME_AT_ONCE - busy(launches)
        for job_id, record in list(launches.items()):
            launch_id = record['launch_id']
            if record['state'] == PARKED:
                # A confirmation may have woken it after the snapshot; RESTART_MESSAGE must never reach a live session.
                if (not record['parked_request'] or state_of.get(record['parked_request']) == 'open' or store.control('draining')
                        or launcher.reconcile(launch_id)['state'] != PARKED):
                    continue
                if slots <= 0:
                    continue
                slots -= 1
                try:
                    launches[job_id] = launcher.restart(launch_id)
                except RuntimeError as exc:
                    store.alert(job_id, 'resume_failed', f'The Applier was parked to save memory and did not restart: {exc}'[:300])
                    events.append({'event': 'parked_session_not_resumed', 'job_id': job_id, 'error': str(exc)[:200]})
                    continue
                events.append({'event': 'parked_session_resumed', 'job_id': job_id})
                continue
            job_requests = [r for r in requests if r['job_id'] == job_id]
            if parking(record, job_requests, store.submit_state(job_id), job_id in finished, now) is None:
                continue
            due = parking(launcher.reconcile(launch_id), job_requests, store.submit_state(job_id), job_id in finished, now)
            if due is None:
                continue
            launches[job_id] = launcher.park(launch_id, due.awaiting)
            if launches[job_id]['state'] == PARKED:
                events.append({'event': 'idle_session_parked', 'job_id': job_id, 'awaiting': due.awaiting})
    return events


def stop_finished(launcher, launches, now):
    """Stop the session of each job that is over: its submission recorded in Jobfeed, or its skip or closed posting handled.

    Nothing about such a job waits on the user. A session still working is left alone for CLOSE_GRACE, so it can close its tabs.
    A parked one is stopped for good, so parked always means it comes back.
    launches maps job IDs to reconciled launches; each stopped one is replaced by its record after the stop.
    """
    events = []
    for row in launcher.store.outbox():
        record = launches.get(row['job_id'])
        if row['state'] != 'done' or record is None or not (has_process(record) or record['state'] == PARKED):
            continue
        if record.get('cli_state') == 'working' and now - datetime.fromisoformat(row['created_at']) < CLOSE_GRACE:
            continue
        launches[row['job_id']] = launcher.stop(record['launch_id'])
        if not has_process(launches[row['job_id']]):
            events.append({'event': 'finished_session_stopped', 'job_id': row['job_id']})
    return events


def tick(launcher, *, now=None, sync=None, skip=None, closed=None):
    """Run one supervision pass and return the events it produced."""
    now = now or datetime.now(timezone.utc)
    store, events = launcher.store, []
    handlers = {kind: handler for kind, handler in (('jobfeed_sync', sync), ('user_skip', skip), ('posting_closed', closed)) if handler is not None}
    if handlers:
        events += drain_outbox(store, handlers)
    for request in store.escalations():
        launch_id = launcher.launch_for(request['job_id'])
        if launch_id is None:
            continue
        # Only an Applier that ended its own turn is escalated. A stopped one was stopped on purpose. Told to end its turn,
        # an idle session may stay listed as blocked, so it is stopped here first; a working one is left for a later pass.
        record = launcher.reconcile(launch_id)
        if record.get('cli_state') == 'blocked':
            ended = launcher.stop(launch_id)['state'] in ENDED
        else:
            ended = record['state'] == 'finished'
        if ended:
            launcher.escalate(launch_id, dict(request, evidence=json.loads(request['evidence']) if request['evidence'] else None))
            events.append({'event': 'escalated', 'job_id': request['job_id'], 'field': request['field']})
    for message in store.pending_messages():
        launch_id = launcher.launch_for(message['job_id'])
        if launch_id is None or not store.claim_message(message['id']):
            continue
        try:
            outcome = launcher.wake(launch_id, message['text'])
        except RuntimeError as exc:
            store.release_message(message['id'])
            events.append({'event': 'message_not_delivered', 'job_id': message['job_id'], 'error': str(exc)[:200]})
            continue
        if outcome in {'messaged', 'restarted'}:
            store.message_delivered(message['id'], outcome)
            events.append({'event': 'message_delivered', 'job_id': message['job_id'], 'outcome': outcome})
        else:
            store.release_message(message['id'])
    launches = {}
    for job in store.jobs():
        launch_id = launcher.launch_for(job['job_id'])
        if launch_id is not None:
            launches[job['job_id']] = launcher.reconcile(launch_id)
    # A skipped job's parked Applier comes back to close its tabs before stop_finished would stop it for good.
    events += park_idle(launcher, launches, now)
    events += stop_finished(launcher, launches, now)
    for job_id, kind, detail in watch(store, launches, now):
        if store.alert(job_id, kind, detail):
            events.append({'event': 'alert', 'job_id': job_id, 'kind': kind, 'detail': detail})
    return events


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    launcher = ClaudeApplierLauncher(args.root)
    while True:
        for event in tick(launcher):
            print(json.dumps(event), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
