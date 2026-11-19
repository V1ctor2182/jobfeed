"""CAPTCHA holds: a job whose Applier met a bot check waits in the queue for the user, while the batch goes on.

The Applier reports the check with report_blocker and ends its turn. The worker then holds the job in its queue row, so
no dispatch path picks it and it takes no slot. With no Submit attempt, once the session has ended its Applier is retired and the job goes back to
Queued as a requeue does, still listed by its batch. After a Submit attempt the click may have reached the employer, so the
session and its tab stay and only the slot is released. The user solves the check in this computer's Chrome and clears
the hold from Discord or the CLI. A Queued job then starts again in its normal turn; a kept Applier is woken to look at its
tab again. No agent ever solves a CAPTCHA.

The same pass returns a job whose form tab was lost before any Submit attempt to Queued, with no hold: nothing was sent,
so a new Applier fills it again from the start in its normal turn.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline.applier_runtime import RETRYABLE_SUBMIT, ApplierRuntime
from pipeline.claude_dispatch import is_claude_owner
from pipeline.lifecycle import ApplicationState
from pipeline.store import ApplicationQueue, Hold
from pipeline.submission_reservation import admission_lock

WORKER = 'worker: CAPTCHA hold'
CLI_ACTOR = 'user via pipeline.cli captcha clear'
REASON = 'The Applier met a CAPTCHA before any Submit attempt. The job waits in the queue until the user solves it.'
LOST_TAB_WORKER = 'worker: lost form tab'
LOST_TAB_REASON = "The Applier's form tab was lost before it clicked Submit, so a new Applier fills the job again."
REQUEUES = {'captcha': (REASON, WORKER), 'lost_tab': (LOST_TAB_REASON, LOST_TAB_WORKER)}
SOLVED_MESSAGE = ('The user solved the CAPTCHA. Call tabs_context_mcp and find your registered tab, check the page again, and continue from '
                  'where you stopped. If a CAPTCHA or bot check still shows, call report_blocker with blocker_type captcha again and end your turn.')


def park(pipeline, launcher, in_flight=frozenset()):
    """One worker pass: hold each new CAPTCHA report, return held jobs and jobs that lost their tab to Queued when they never
    tried Submit, and wake each kept Applier whose hold the user cleared. Jobs with application work in flight wait for a later pass."""
    from pipeline.requeue import requeue
    root, queue, events = pipeline.config.resume_root, pipeline.queue, []
    store = ApplierRuntime(root)
    with admission_lock(root):
        owners = {job['job_id']: job['owner'] for job in store.jobs()}
        for report in store.captcha_reports():
            job_id = report['job_id']
            item = queue.get(job_id)
            if item is None or item.codex_application_session_id != owners.get(job_id):
                continue
            if item.hold is not None and item.hold.id == report['id']:
                if not item.hold.active and store.captcha_solved(report['id'], SOLVED_MESSAGE):
                    events.append({'event': 'captcha_retry', 'job_id': job_id, 'cleared_by': item.hold.cleared_by})
                continue
            page_url = (store.request(report['id'])['evidence'] or {}).get('page_url')
            if queue.hold(job_id, Hold(report['id'], 'captcha', report['text'], page_url, datetime.now(timezone.utc).isoformat())):
                events.append({'event': 'captcha_held', 'job_id': job_id})
        returning = {item.job_id: ('captcha', item) for item in queue.list_active() if item.held}
        for report in store.lost_tabs():
            if (item := queue.get(report['job_id'])) is not None:
                returning.setdefault(item.job_id, ('lost_tab', item))
        for kind, item in returning.values():
            owner = item.codex_application_session_id
            if item.state != ApplicationState.APPLYING or not is_claude_owner(owner) or item.job_id in in_flight:
                continue
            submit = store.submit_state(item.job_id)
            if submit is not None and submit['state'] not in RETRYABLE_SUBMIT:
                continue
            # The Applier was told to end its turn, but `claude agents` may still report its idle session blocked, which
            # requeue reads as running. A working session is left for a later pass.
            launch_id = launcher.launch_for(item.job_id)
            if launch_id is not None and launcher.reconcile(launch_id).get('cli_state') == 'blocked':
                launcher.stop(launch_id)
            reason, actor = REQUEUES[kind]
            result = requeue(pipeline.config, [item.job_id], reason, worker_busy=lambda _: False, inventory=launcher.agents,
                             actor=actor, keep_batches=True)[0]
            events.append({'event': f'{kind}_requeued' if result['status'] == 'requeued' else f'{kind}_requeue_refused', 'job_id': item.job_id,
                           **({'reason': result['reason']} if 'reason' in result else {})})
    return events


def clear(root, jobfeed_db, job_id, cleared_by, hold_id=None):
    """Clear the job's CAPTCHA hold, or only the hold hold_id. Returns its queue item after the clear, or None when it had no such hold."""
    queue = ApplicationQueue(jobfeed_db)
    with admission_lock(root):
        return queue.get(job_id) if queue.clear_hold(job_id, cleared_by, hold_id) else None


def describe(item):
    hold = item.hold
    return {'job_id': item.job_id, 'state': item.state.value, 'kind': hold.kind, 'detail': hold.detail, 'page_url': hold.page_url,
            'since': hold.created_at, 'applier': 'kept, woken when cleared' if item.codex_application_session_id else 'retired, starts again when cleared'}


def held(config):
    return [describe(item) for item in ApplicationQueue(config.jobfeed_db).list_all() if item.held]


def clear_jobs(config, job_ids):
    results = []
    for job_id in job_ids:
        item = clear(config.resume_root, config.jobfeed_db, job_id, CLI_ACTOR)
        results.append({'job_id': job_id, 'status': 'cleared', 'cleared_at': item.hold.cleared_at} if item else {'job_id': job_id, 'status': 'not_held'})
    return results
