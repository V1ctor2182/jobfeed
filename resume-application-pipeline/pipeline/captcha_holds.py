"""CAPTCHA holds: a job whose Applier met a bot check waits in the queue for the user, while the batch goes on.

The Applier reports the check with report_blocker and ends its turn. The worker then holds the job in its queue row, so
no dispatch path picks it and it takes no slot. With no Submit attempt, once the session has ended its Applier is retired and the job goes back to
Queued as a requeue does, still listed by its batch. After a Submit attempt the click may have reached the employer, so the
session and its tab stay and only the slot is released. The user solves the check in this computer's Chrome and clears
the hold from Discord or the CLI. A Queued job then starts again in its normal turn; a kept Applier is woken to look at its
tab again. No agent ever solves a CAPTCHA.

The same pass returns a job whose form tab was lost before any Submit attempt to Queued, with no hold: nothing was sent,
so a new Applier fills it again from the start in its normal turn. A job whose tab was lost after a Submit attempt nobody
saw the outcome of goes back too, by the user's standing rule (2026-11-18), once the confirmation mail check found no
email from the employer. Its new Applier first looks on the site for an earlier application.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.applier_runtime import ATTEMPTED, ENDED_RETRY_ACTOR, RETRY_ACTOR, RETRYABLE_SUBMIT, ApplierRuntime
from pipeline.claude_dispatch import is_claude_owner
from pipeline.lifecycle import ApplicationState
from pipeline.store import ApplicationQueue, Hold
from pipeline.submission_reservation import admission_lock

WORKER = 'worker: CAPTCHA hold'
CLI_ACTOR = 'user via pipeline.cli captcha clear'
REASON = 'The Applier met a CAPTCHA before any Submit attempt. The job waits in the queue until the user solves it.'
LOST_TAB_WORKER = 'worker: lost form tab'
LOST_TAB_REASON = "The Applier's form tab was lost before it clicked Submit, so a new Applier fills the job again."
RETRY_REASON = ("The Applier's form tab was lost after a Submit attempt whose outcome nobody saw, and no confirmation email came, "
                "so a new Applier applies again after checking the site for an earlier application.")
ENDED_WORKER = 'worker: Applier ended unfinished'
ENDED_REASON = 'The Applier ended its session before Submit, with nothing waiting on the user, so a new Applier fills the job again.'
ENDED_RETRY_REASON = ("The Applier ended after a Submit attempt whose outcome nobody saw, and no confirmation email came, so a new "
                      "Applier applies again after checking the site for an earlier application.")
REQUEUES = {'captcha': (REASON, WORKER), 'lost_tab': (LOST_TAB_REASON, LOST_TAB_WORKER), 'lost_after_submit': (RETRY_REASON, RETRY_ACTOR),
            'ended': (ENDED_REASON, ENDED_WORKER), 'ended_after_submit': (ENDED_RETRY_REASON, ENDED_RETRY_ACTOR)}
RETRIED = frozenset({'lost_after_submit', 'ended_after_submit'})
# An ended session may still be restarted by a message or an escalation in the minutes after it ends.
ENDED_GRACE = timedelta(minutes=30)
# Requests that mean the job waits on the user or the supervisor. An open Submit outcome is the mail check's, not the user's.
_WAITING = frozenset({'open', 'escalated', 'escalation_sent'})
_DONE_STEPS = frozenset({'ready for review', 'submitted'})


def ended_runs(store, now):
    """(kind, job_id) for each job whose Applier ended on its own and that nothing else will move again.

    2026-11-18: 32 jobs sat in Applying for days: 9 Appliers had ended before Submit with nothing open, and 23 had ended
    after a Submit attempt whose outcome nobody saw. 'ended' sent nothing. 'ended_after_submit' is the user's standing rule:
    once the confirmation mail check found no email, the job is applied to again. A job waiting at review, on a question
    or an escalation, or on a mail check that has not answered yet is left alone."""
    requests, jobs = store.requests(), {job['job_id']: job for job in store.jobs()}
    unconfirmed, replies = store.unconfirmed_by_email(), store.last_replies()
    found = []
    for launch in store.ended_launches():
        job_id, job = launch['job_id'], jobs.get(launch['job_id']) or {}
        # A launch record is rewritten on every reconcile, so the job's own last activity dates the end.
        active = [at for at in (job.get('last_progress_at'), launch.get('resumed_at'), replies.get(job_id)) if at]
        if (active and now - max(map(datetime.fromisoformat, active)) < ENDED_GRACE) or job.get('last_progress') in _DONE_STEPS:
            continue
        waiting = [r for r in requests if r['job_id'] == job_id and r['state'] in _WAITING]
        submit = store.submit_state(job_id)
        if submit is None or submit['state'] in RETRYABLE_SUBMIT:
            if not waiting:
                found.append(('ended', job_id))
        elif submit['state'] in ATTEMPTED and job_id in unconfirmed and all(r['blocker_type'] == 'submit_unknown' for r in waiting):
            found.append(('ended_after_submit', job_id))
    return found
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
        from pipeline.confirmation_mail import no_email_key
        for report in store.lost_after_submit():
            if store.control(no_email_key(report['id'])) is not None and (item := queue.get(report['job_id'])) is not None:
                returning.setdefault(item.job_id, ('lost_after_submit', item))
        from pipeline.receipts import settle
        events += settle(store)
        events += try_company_sites(pipeline, store, queue)
        for kind, job_id in ended_runs(store, datetime.now(timezone.utc)):
            if (item := queue.get(job_id)) is not None and not item.held and item.codex_application_session_id == owners.get(job_id):
                returning.setdefault(job_id, (kind, item))
        reported = vars(pipeline).setdefault('requeue_refusals_reported', set())
        for kind, item in returning.values():
            owner = item.codex_application_session_id
            if item.state != ApplicationState.APPLYING or not is_claude_owner(owner) or item.job_id in in_flight:
                continue
            submit = store.submit_state(item.job_id)
            if submit is not None and submit['state'] not in RETRYABLE_SUBMIT and kind not in RETRIED:
                continue
            # The Applier was told to end its turn, but `claude agents` may still report its idle session blocked, which
            # requeue reads as running. A working session is left for a later pass.
            launch_id = launcher.launch_for(item.job_id)
            if launch_id is not None and launcher.reconcile(launch_id).get('cli_state') == 'blocked':
                launcher.stop(launch_id)
            reason, actor = REQUEUES[kind]
            result = requeue(pipeline.config, [item.job_id], reason, worker_busy=lambda _: False, inventory=launcher.agents,
                             actor=actor, keep_batches=True, retry_attempt=kind in RETRIED)[0]
            if result['status'] != 'requeued' and kind.startswith('ended'):
                # A job with submission or skip records stays where it is. Report that once, not on every pass.
                if (item.job_id, result.get('reason')) in reported:
                    continue
                reported.add((item.job_id, result.get('reason')))
            events.append({'event': f'{kind}_requeued' if result['status'] == 'requeued' else f'{kind}_requeue_refused', 'job_id': item.job_id,
                           **({'reason': result['reason']} if 'reason' in result else {})})
    return events


COMPANY_SITE = 'worker: try the employer site instead of Indeed'
COMPANY_SITE_TRIED = 'company-site-{job_id}'


def try_company_sites(pipeline, store, queue):
    """Clear, once per job, the CAPTCHA hold of an Indeed job that met it before any Submit attempt, so its next Applier
    looks for the role on the employer's own site first (the user's choice, 2026-11-19: 61 of 75 held jobs were Indeed's).
    Nothing gets past the CAPTCHA: the new run takes another route, and a CAPTCHA it meets again waits for the user."""
    events = []
    for item in queue.list_active():
        job_id, hold = item.job_id, item.hold
        if (not item.held or hold.kind != 'captcha' or item.codex_application_session_id is not None or item.state != ApplicationState.QUEUED
                or 'indeed.com' not in (hold.page_url or '') or store.control(COMPANY_SITE_TRIED.format(job_id=job_id)) is not None):
            continue
        store.set_control(COMPANY_SITE_TRIED.format(job_id=job_id), hold.id)
        if queue.clear_hold(job_id, COMPANY_SITE, hold.id) is not None:
            events.append({'event': 'captcha_company_site_retry', 'job_id': job_id})
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
