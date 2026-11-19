"""Start and recover Claude CLI Appliers from the existing application pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import queue
from pathlib import Path
import re
import threading
import time
import uuid

from pipeline.claude_applier import ClaudeApplierLauncher, never_started
from pipeline.lifecycle import TERMINAL_STATES, ApplicationState

# Seconds between checks of Gmail for the confirmation of a Submit whose outcome an Applier could not see.
MAIL_EVERY = 600.0
MAIL_ENV = 'JOBFEED_CONFIRMATION_MAIL'
TABS_EVERY = 600.0
# An Applier idle this long with nothing open has ended its turn. Long enough that one between two steps keeps its slot.
IDLE_FREES_SLOT = timedelta(minutes=10)
_CLAUDE_OWNER = re.compile(r'[0-9a-f]{32}\Z')


def is_claude_owner(owner):
    return bool(owner) and bool(_CLAUDE_OWNER.fullmatch(owner))


def launcher_for(pipeline):
    return ClaudeApplierLauncher(pipeline.config.resume_root)


def _scope(pipeline):
    return str(getattr(pipeline, 'manifest_path', pipeline.applications.execution_scope))


def _url(pipeline, job):
    snapshot = getattr(pipeline, 'candidate_snapshots', {}).get(job.id, {})
    return snapshot.get('application_url') or snapshot.get('final_url') or job.url


def _launch(pipeline, job, folder, launch_id):
    return launcher_for(pipeline).launch(
        job.id, scope=_scope(pipeline), url=_url(pipeline, job), materials=[folder], work_dir=folder / 'claude-applier',
        jobfeed_db=pipeline.config.jobfeed_db, dashboard_url=getattr(pipeline, 'dashboard_url', None), launch_id=launch_id,
        evidence_dir=folder, job_url=job.url, company=getattr(job, 'company', None))


def start_claude_application(pipeline, item, job, folder):
    """Record the owner and execution binding, then launch the Applier."""
    from pipeline.submission_reservation import admission_lock, assert_available
    root = pipeline.config.resume_root
    existing = launcher_for(pipeline).launch_for(job.id)
    if existing is not None:
        record = launcher_for(pipeline).reconcile(existing)
        return {'job_id': job.id, 'status': 'claude_applier_running', 'launch_id': existing, 'state': record['state']}
    launch_id = uuid.uuid4().hex
    with admission_lock(root):
        assert_available(root, job.id)
        current = pipeline.queue.get(job.id)
        if current is not None and current.codex_application_session_id:
            raise ValueError('job already has an application owner')
        pipeline.queue.transition(job.id, ApplicationState.APPLYING, codex_application_session_id=launch_id)
        pipeline.applications.execution_gate.bind(_scope(pipeline), job.id, launch_id)
    record = _launch(pipeline, job, folder, launch_id)
    return {'job_id': job.id, 'status': 'claude_applier_started', 'launch_id': launch_id, 'state': record['state']}


def sync_claude_application(pipeline, job_id):
    """Recovery for a Claude-owned job: relaunch under the same owner if the launch never happened, else reconcile."""
    item = pipeline.queue.get(job_id)
    if item is None or not is_claude_owner(item.codex_application_session_id):
        raise ValueError('job is not owned by a Claude Applier')
    launcher = launcher_for(pipeline)
    launch_id = launcher.launch_for(job_id)
    if launch_id is None:
        record = _launch(pipeline, pipeline.jobfeed.get_job(job_id), pipeline._folder(item), item.codex_application_session_id)
        return {'job_id': job_id, 'status': 'claude_applier_relaunched', 'state': record['state']}
    if launch_id != item.codex_application_session_id:
        raise ValueError('Claude launch does not match the queue owner')
    record = launcher.retry(launch_id)
    return {'job_id': job_id, 'status': 'claude_applier_observed', 'state': record['state']}


def active_claude_owners(pipeline, owners):
    """Claude owners whose session is running.

    An owner without a launch record, or with a launch that never produced a session, holds no slot, so the worker's
    sync can start it under the same owner. `claude --bg` runs only after its launch record is written.
    """
    launcher = launcher_for(pipeline)
    known = set(launcher.launches())

    def holds_slot(record):
        return record['state'] in {'running', 'unknown', 'requested', 'launched'} and not never_started(record)
    return {owner for owner in owners if owner in known and holds_slot(launcher.reconcile(owner))}


def waiting_claude_owners(pipeline, owners):
    """Claude owners idle on an unanswered question or blocker, or idle for IDLE_FREES_SLOT after ending their turn.
    They keep their session but hold no slot.

    An escalated blocker waits on the supervisor, not the user, and a session still working keeps its slot. An Applier
    that got its reply and ended its turn without acting has nothing open, and held its slot until it was stopped
    (2026-11-12: six of them held all four slots for five hours).
    """
    launcher = launcher_for(pipeline)
    requests = launcher.store.requests()
    asking = {request['job_id'] for request in requests if request['state'] == 'open'}
    escalated = {request['job_id'] for request in requests if request['state'] == 'escalated'}
    jobs = {job['owner']: job['job_id'] for job in launcher.store.jobs()}
    now = datetime.now(timezone.utc)

    known = set(launcher.launches())

    def waits(owner):
        if owner not in known:
            return False
        record = launcher.reconcile(owner)
        if record.get('cli_state') != 'blocked':
            return False
        if jobs.get(owner) in asking:
            return True
        idle_since = record.get('idle_since')
        return bool(idle_since) and jobs.get(owner) not in escalated and now - datetime.fromisoformat(idle_since) >= IDLE_FREES_SLOT
    return {owner for owner in owners if waits(owner)}


def stop_claude_applier(pipeline, owner):
    launcher = launcher_for(pipeline)
    if owner in set(launcher.launches()):
        return launcher.stop(owner)['state'] in {'stopped', 'finished', 'failed'}
    return False


OK_SYNC = frozenset({'submission_completed', 'submission_cleanup_pending', 'jobfeed_synced'})


def jobfeed_sync(pipeline):
    """Outbox callback: record a verified Claude submission in Jobfeed. Anything short of confirmed sync raises."""
    def sync(job_id):
        result = pipeline.finish_submission(job_id)
        if result.get('status') not in OK_SYNC:
            raise RuntimeError(f"Jobfeed sync not confirmed: {result.get('status')} {result.get('reason', '')}".strip())
        return result
    return sync


def _drop(pipeline, job_id, end, prefix):
    """End the job's queue item, which frees its slot, then mark it ignored in Jobfeed. Safe to repeat.

    The supervisor stops the job's Applier once it has closed its tabs."""
    record = launcher_for(pipeline).store.skip(job_id)
    item = pipeline.queue.get(job_id)
    if item is not None and item.state not in TERMINAL_STATES:
        end(job_id)
    if not pipeline.jobfeed.is_ignored(job_id):
        pipeline.jobfeed.record_ignored(job_id, record['reason'], prefix=prefix)


def jobfeed_skip(pipeline):
    """Outbox callback for a skip the user asked for: the job is cancelled."""
    return lambda job_id: _drop(pipeline, job_id, pipeline.cancel, 'User asked to skip this posting: ')


def jobfeed_closed(pipeline):
    """Outbox callback for a posting the Applier found closed: the job is unavailable, as for any closed posting."""
    return lambda job_id: _drop(pipeline, job_id, lambda j: pipeline.queue.transition(j, ApplicationState.UNAVAILABLE), 'Employer posting closed: ')


class ClaudeServices:
    """Run the supervisor and the Discord bridge inside the worker loop."""

    def __init__(self, pipeline, *, clock=time.monotonic, supervise_every=60.0, bridge_every=5.0, bridge_factory=None, env_path=None):
        from pipeline.applier_discord import DEFAULT_ENV, bridge_from_env
        self.pipeline, self.clock = pipeline, clock
        self.supervise_every, self.bridge_every = supervise_every, bridge_every
        self.env_path = Path(env_path or DEFAULT_ENV).expanduser()
        self.bridge_factory = bridge_factory or (lambda: bridge_from_env(pipeline.config.resume_root, self.env_path, pipeline.config, listen=True))
        self._bridge, self._next_supervise, self._next_bridge, self._drained = None, 0.0, 0.0, False
        # Only the autopilot service's unit turns this on, so no test reaches Gmail.
        self.mail_every = MAIL_EVERY if os.environ.get(MAIL_ENV) else None
        self._mail, self._next_mail, self._mail_events, self._mail_tried = None, 0.0, queue.SimpleQueue(), set()
        # The same unit switch keeps tests away from the applying browser.
        self.tabs_every = TABS_EVERY if os.environ.get(MAIL_ENV) else None
        self._tabs, self._next_tabs = None, 0.0
        if os.environ.get(MAIL_ENV):
            from pipeline.apply_browser import ExtensionGate
            pipeline.browser_gate = ExtensionGate()

    def tick(self, in_flight=frozenset()):
        """in_flight is the jobs whose application work the worker is running, which no hold may requeue under it."""
        from pipeline.applier_supervisor import tick as supervise
        from pipeline.captcha_holds import park
        from pipeline import site_holds
        events, now = [], self.clock()
        if now >= self._next_supervise:
            self._next_supervise = now + self.supervise_every
            events += park(self.pipeline, launcher_for(self.pipeline), in_flight)
            events += site_holds.park(self.pipeline, launcher_for(self.pipeline).store)
            events += supervise(launcher_for(self.pipeline), sync=jobfeed_sync(self.pipeline), skip=jobfeed_skip(self.pipeline), closed=jobfeed_closed(self.pipeline))
        if now >= self._next_bridge and self.env_path.exists():
            self._next_bridge = now + self.bridge_every
            if self._bridge is None:
                self._bridge = self.bridge_factory()
            events += self._bridge.sync()
        # A Gmail call takes tens of seconds, so the check runs beside the worker loop and reports on a later tick.
        if self.mail_every and now >= self._next_mail and (self._mail is None or not self._mail.is_alive()):
            self._next_mail = now + self.mail_every
            self._mail = threading.Thread(target=self._check_mail, daemon=True)
            self._mail.start()
        # Reading every tab's history takes a few seconds, so this also runs beside the worker loop.
        if self.tabs_every and now >= self._next_tabs and (self._tabs is None or not self._tabs.is_alive()):
            self._next_tabs = now + self.tabs_every
            self._tabs = threading.Thread(target=self._close_done_tabs, daemon=True)
            self._tabs.start()
        while not self._mail_events.empty():
            events.append(self._mail_events.get())
        gate = getattr(self.pipeline, 'browser_gate', None)
        if gate is not None:
            events += gate.drain()
        return events

    def _close_done_tabs(self):
        from pipeline.browser_broker import Chromium
        from pipeline.tab_cleanup import sweep
        chromium = Chromium()
        try:
            closed = sweep(launcher_for(self.pipeline).store, self.pipeline.config.jobfeed_db, chromium.session())
            if closed:
                self._mail_events.put({'event': 'done_tabs_closed', 'tabs': len(closed), 'job_ids': sorted({j for page in closed for j in page['job_ids']})})
        except Exception as exc:  # noqa: BLE001 - a failed pass must not stop the worker; the next one tries again
            self._mail_events.put({'event': 'tab_cleanup_error', 'error': f'{type(exc).__name__}: {exc}'[:300]})
        finally:
            chromium.drop()

    def _check_mail(self):
        from pipeline.confirmation_mail import check
        try:
            found = check(self.pipeline, launcher_for(self.pipeline).store, tried=self._mail_tried)
        except Exception as exc:  # noqa: BLE001 - a failed check must not stop the worker; the next one tries again
            found = [{'event': 'confirmation_mail_error', 'error': str(exc)[:300]}]
        for event in found:
            self._mail_events.put(event)

    def drain(self):
        """Called once when the user stops the worker: no new Appliers, waiting ones stop at their safe point."""
        if self._drained:
            return None
        self._drained = True
        return launcher_for(self.pipeline).drain()
