"""Long-running producer and Claude Applier fan-out worker for one batch."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable

from pipeline.lifecycle import ApplicationState, TERMINAL_STATES, WAITING_ON_USER
from pipeline.orchestrator import ApplicationPipeline


RECEIPT_ONLY_ERRORS = frozenset({'missing_submission_evidence', 'submission_claim_requires_receipt'})
RECOVERY_ACTIONS = frozenset({'submission_cleanup', 'submission_reconciled'})


@dataclass(frozen=True)
class WorkerSettings:
    queue_target: int = 100
    application_workers: int = 4
    poll_interval: float = 1.0
    recovery_interval: float = 10.0

    def __post_init__(self) -> None:
        if self.application_workers < 1:
            raise ValueError("worker counts must be positive")


class OrchestratorWorker:
    """Keep the batch's loops running without serializing independent work."""

    def __init__(
        self,
        pipeline: ApplicationPipeline,
        settings: WorkerSettings | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        progress_monitor=None,
    ):
        self.pipeline = pipeline
        from pipeline.claude_dispatch import ClaudeServices
        self._claude_services = ClaudeServices(pipeline)
        self.progress_monitor = progress_monitor
        if hasattr(getattr(pipeline, 'applications', None), 'execution_scope') and progress_monitor:
            pipeline.applications.execution_scope = str(getattr(progress_monitor.sources, 'manifest', 'replay'))
        self._progress_snapshot = None
        self._progress_config = getattr(pipeline, 'loaded_manifest_hash', None)
        self._progress_error = None
        self._progress_done = False
        self.settings = settings or WorkerSettings()
        self.emit = emit or _print_event
        pipeline.emit = self.emit
        self._stranded_adopted = False
        self._producer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="priority")
        self._applications = ThreadPoolExecutor(
            max_workers=self.settings.application_workers,
            thread_name_prefix="application",
        )
        self._producer_future: Future[Any] | None = None
        self._application_futures: dict[int, tuple[str, Future[Any]]] = {}
        self._last_recovery: dict[int, float] = {}
        self._next_producer_attempt = 0.0
        # Claude owners whose Applier works, from the last scheduling pass; None until one has counted them.
        self._busy_owners: set[str] | None = None
        self._submission_interrupt_attempted = set()

    def run_forever(self) -> None:
        import fcntl
        handle = None
        try:
            # All entry points share one writer lock, including unsupervised launches.
            path = self.pipeline.config.resume_root / '.pipeline/worker-progress.lock'
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open('a')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._worker_lock_handle = handle
            if self._user_stopped():
                self.emit({'event': 'user_stopped', 'next': 'no worker initialization or dispatch'})
                return
            self.emit({"event": "orchestrator_started", "queue_target": self.settings.queue_target})
            busy_attempts = 0
            while True:
                try:
                    self.tick()
                except sqlite3.OperationalError as exc:
                    # A contended write is not a dead worker. Keep the same
                    # futures/owners and retry the durable queue on a later tick.
                    # Never turn unrelated SQLite errors into silent retries.
                    code = getattr(exc, 'sqlite_errorcode', None)
                    if not ((code is not None and (code & 0xff) in
                             (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)) or
                            (code is None and str(exc).lower() in
                             {'database is locked', 'database table is locked'})):
                        raise
                    busy_attempts += 1
                    if busy_attempts == 1 or busy_attempts % 12 == 0:
                        self.emit({'event': 'database_contention',
                                   'attempts': busy_attempts, 'error': str(exc),
                                   'next': 'retain existing owners; retry queue after bounded delay'})
                    # Check stop before retrying, including while contention persists.
                    if self._user_stopped():
                        self.emit({'event': 'user_stopped', 'next': 'drain existing work; no database retry'})
                        break
                    time.sleep(min(5.0, max(0.1, self.settings.poll_interval) * min(busy_attempts, 5)))
                    continue
                if busy_attempts:
                    self.emit({'event': 'database_contention_recovered', 'attempts': busy_attempts})
                    busy_attempts = 0
                if self._progress_done:
                    break
                time.sleep(self.settings.poll_interval)
        except KeyboardInterrupt:
            self.emit({"event": "orchestrator_draining", "next": "等待已有 turn 结束；未退出前禁止启动第二 worker"})
        finally:
            self.close()
            self.emit({"event": "orchestrator_stopped"})
            if handle:
                from pipeline.dashboard_submission import finish_worker
                for receipt in finish_worker(self, handle):
                    self.emit({'event': 'dashboard_submission', **receipt})

    def tick(self) -> None:
        if self._user_stopped():
            self._progress_done = True
            self.emit({'event': 'user_stopped', 'next': 'drain existing work; do not dispatch or restart'})
            drained = self._claude_services.drain()
            if drained is not None:
                self.emit({'event': 'claude_appliers_draining', **drained})
            return
        self._cancel_confirmed_submission_work()
        self.pipeline.initialize()
        if not self._stranded_adopted and getattr(self.pipeline, 'continuous', False) is True:
            # This must finish before anything below can start a Claude session. See adopt_stranded. A failure leaves the
            # tick here: the run loop retries a contended database, and any other error ends the worker. While the recruiter
            # sheet cannot be read nothing below starts a job either, so the tick goes on and retries this on the next one.
            from pipeline.autopilot import adopt_stranded
            self._stranded_adopted = adopt_stranded(self.pipeline) is not None
        self._reap()
        try:
            for event in self._claude_services.tick(set(self._application_futures)):
                self.emit({'source': 'claude_applier', **event})
        except Exception as exc:  # Claude services must never stop the worker loop.
            self.emit({'event': 'claude_services_error', 'error': str(exc)[:300]})
        from pipeline.dashboard_submission import consume_worker
        for receipt in consume_worker(self):
            self.emit({'event': 'dashboard_submission', **receipt})
        for job_id in self.pipeline.process_completions():
            self.emit({"event": "resume_ready", "job_id": job_id})
        self._check_progress()
        if self._progress_done:
            return
        if self.progress_monitor and not self._progress_snapshot:
            return  # Evidence source unavailable; do not dispatch on stale labels.
        self._schedule_applications()
        self._schedule_producer()

    def _cancel_confirmed_submission_work(self):
        if not hasattr(type(self.pipeline), '_user_submission'):
            return
        for job_id, (action, future) in list(self._application_futures.items()):
            if action in RECOVERY_ACTIONS or not self.pipeline._user_submission(job_id):
                continue
            if future.cancel():
                del self._application_futures[job_id]
                self.emit({'event': 'confirmed_submission_cancelled_queued_work', 'job_id': job_id, 'action': action})
            elif not future.done() and job_id not in self._submission_interrupt_attempted:
                item = self.pipeline.queue.get(job_id)
                from pipeline.claude_dispatch import is_claude_owner, stop_claude_applier
                if item and is_claude_owner(item.codex_application_session_id):
                    self._submission_interrupt_attempted.add(job_id)
                    try:
                        sent = stop_claude_applier(self.pipeline, item.codex_application_session_id)
                        self.emit({'event':'confirmed_submission_interrupt_requested', 'job_id':job_id, 'sent':bool(sent), 'verified_stopped':False})
                    except Exception as exc:
                        self.emit({'event':'confirmed_submission_interrupt_unknown', 'job_id':job_id, 'error':str(exc)})

    def _user_stopped(self):
        from pipeline.worker_recovery import stopped
        manifest = getattr(self.pipeline, 'manifest_path', None)
        is_stopped = stopped(manifest) if isinstance(manifest, (str, Path)) else False
        if is_stopped and not getattr(self, '_stop_fenced', False):
            gate = getattr(getattr(self.pipeline, 'applications', None), 'execution_gate', None)
            if gate:
                gate.cancel_scope(str(manifest), 'batch-handoff:stopped_by_user')
            if self.progress_monitor and self.progress_monitor.ledger:
                self.progress_monitor.ledger.db.execute('UPDATE dq_scopes SET active=0 WHERE scope=?',(str(manifest),))
            self._stop_fenced = True
        return is_stopped

    def _recovery_budget(self):
        from pipeline.worker_recovery import RecoveryBudget
        import hashlib
        root = getattr(getattr(self.pipeline, 'config', None), 'resume_root', None)
        if not isinstance(root, (str, Path)):
            return None
        binding = str(getattr(self.pipeline, 'manifest_path', None) or Path(root).resolve())
        name = hashlib.sha256(binding.encode()).hexdigest()[:16]
        return RecoveryBudget(Path(root)/'.pipeline/worker-recovery'/f'{name}.json', binding)

    def _recovery_allowed(self, action, job_id):
        if self._user_stopped():
            return False
        budget = self._recovery_budget()
        return not budget or budget.allowed(f'{action}:{job_id}')

    def _check_progress(self):
        if not self.progress_monitor:
            return
        try:
            snapshot = self.progress_monitor.tick()
            if snapshot is not None:
                if self._progress_config is None:
                    self._progress_config = snapshot['config_hash']
                if self._progress_config != snapshot['config_hash']:
                    # Apply the edited manifest in place; stop only if that fails
                    # or the loaded file still differs from what the monitor read.
                    reload = getattr(self.pipeline, 'reload_manifest', None)
                    if callable(reload):
                        reload()
                    if getattr(self.pipeline, 'loaded_manifest_hash', None) != snapshot['config_hash']:
                        raise ValueError('manifest changed; stop and reload sole worker before dispatch')
                    self._progress_config = snapshot['config_hash']
                    self.emit({'event': 'manifest_reloaded', 'config_hash': snapshot['config_hash']})
                self._progress_snapshot = snapshot
            self._progress_error = None
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._progress_snapshot = None
            error = str(exc)
            if error != self._progress_error:
                self.emit({'event': 'progress_check_failed', 'error': error})
            self._progress_error = error
            return
        from pipeline.submission_reservation import admission_lock
        with admission_lock(self.pipeline.config.resume_root):
            from pipeline.submission_reservation import reservations
            submission_pending = [row for row in reservations(self.pipeline.config.resume_root, scope=getattr(self.pipeline, 'manifest_path', '')) if row['state'] in {'preparing','unknown','queued','running','sync_pending'} or not row.get('terminal_turn') and row['prompt'] is not None]
            snapshot = self._progress_snapshot
            finished = self.pipeline.finished if hasattr(type(self.pipeline), 'finished') else lambda done: done['complete']
            if not submission_pending and snapshot and finished(snapshot) and not self._application_futures:
                self._progress_done = True
                self.emit({'event': 'batch_verified_complete', 'next': 'verify worker exit and pause existing heartbeat'})

    @staticmethod
    def _error_blocked(snap):
        """Jobs stopped by monitor errors, or None when an unprefixed error stops all.

        A "job_id:reason" error stops only that job and its company (2026-10-15
        user decision), so one bad job no longer halts the batch.
        """
        errored, company_wide = set(), set()
        for error in snap.get('dispatch_errors', snap.get('errors', [])):
            head, _, reason = str(error).partition(':')
            if not head.isdigit():
                return None
            errored.add(int(head))
            # A submission claim missing its receipt cannot duplicate another posting.
            if reason not in RECEIPT_ONLY_ERRORS:
                company_wide.add(int(head))
        jobs = snap.get('jobs', [])
        companies = {j.get('company_key') for j in jobs if j['job_id'] in company_wide}
        return errored | {j['job_id'] for j in jobs if j.get('company_key') in companies}

    def _progress_allows(self, action, job_id=None):
        if self._user_stopped():
            return False
        if not self.progress_monitor:
            return True
        snap = self._progress_snapshot
        if not snap:
            return False
        blocked = self._error_blocked(snap)
        if blocked is None or job_id in blocked:
            return False
        job = next((j for j in snap['jobs'] if j['job_id'] == job_id), None)
        if job and job.get('user_submitted') and action == 'submission_reconciled':
            return True  # Read-only Jobfeed reconcile closes the record of a user-finished application.
        if job and (job.get('terminal') or job.get('user_submitted') or not job.get('counted', True) or job['paused'] or job['blocked'] == 'submission_unknown'):
            return False
        if job and action == 'application_started' and snap.get('max_per_company'):
            attempted = sum(bool(j['owner']) for j in snap['jobs'] if j.get('counted', True) and j['company_key'] == job['company_key'] and j['job_id'] != job_id)
            pending = sum(1 for j in snap['jobs'] if j['company_key'] == job['company_key'] and j['job_id'] in self._application_futures and not j['owner'])
            if attempted + pending >= int(snap['max_per_company']):
                return False
        if action == 'application_started':
            c = snap['counts']
            fresh = sum(1 for j in self._application_futures if not any(x['job_id'] == j and (x['reserved'] or x['submitted']) for x in snap['jobs']))
            return c['submitted'] + c['reserved'] + fresh < snap['target']
        return True

    def close(self) -> None:
        self._producer.shutdown(wait=True, cancel_futures=True)
        self._applications.shutdown(wait=True, cancel_futures=True)

    def _reap(self) -> None:
        self._producer_future = self._finish(
            "priority_refill", self._producer_future, retry_delay=15.0
        )
        for job_id, (action, future) in list(self._application_futures.items()):
            if not future.done():
                continue
            self._finish(action, future, job_id=job_id)
            del self._application_futures[job_id]

    def _finish(
        self,
        action: str,
        future: Future[Any] | None,
        *,
        job_id: int | None = None,
        retry_delay: float = 0.0,
    ) -> Future[Any] | None:
        if future is None or not future.done():
            return future
        try:
            result = future.result()
            if action in RECOVERY_ACTIONS:
                budget = self._recovery_budget()
                if budget:
                    key = f'{action}:{job_id}'
                    if isinstance(result, dict) and (result.get('status') in {'submission_cleanup_pending', 'owner_delivery_pending', 'submission_owner_required'} or result.get('cleanup') == 'unknown'):
                        budget.failure(key, json.dumps(result, ensure_ascii=False))
                    else:
                        budget.success(key)
            self.emit({"event": f"{action}_completed", "job_id": job_id, "result": result})
        except BaseException as exc:
            if action in RECOVERY_ACTIONS:
                budget = self._recovery_budget()
                if budget:
                    budget.failure(f'{action}:{job_id}', str(exc))
            self.emit({"event": f"{action}_error", "job_id": job_id, "error": str(exc)})
            if action == "priority_refill":
                self._next_producer_attempt = time.monotonic() + retry_delay
        return None

    def _paused(self, job_id: int | None = None) -> bool:
        if self._user_stopped():
            return True
        check = getattr(self.pipeline.queue, "paused", None)
        return bool(check(job_id)) if callable(check) else False

    def _schedule_producer(self) -> None:
        if self._paused():
            return
        if self._producer_future is not None or time.monotonic() < self._next_producer_attempt:
            return
        from pipeline.claude_dispatch import is_claude_owner
        busy = self._busy_owners

        def in_progress(item):
            if item.state == ApplicationState.QUEUED or item.held:
                return False
            # A Claude job counts only while its Applier works. One parked, ended, or waiting on the user is not work
            # in progress (2026-11-16: 100 such jobs stopped all resume work, and so every new start, for two days).
            owner = item.codex_application_session_id
            return not (busy is not None and is_claude_owner(owner) and owner not in busy)
        active = sum(in_progress(item) for item in self.pipeline.queue.list_active())
        if active >= self.settings.queue_target:
            return
        self._producer_future = self._producer.submit(
            self.pipeline.refill_queue, self.settings.queue_target
        )
        self.emit({"event": "priority_refill_started", "active": active})

    def _active_application_threads(self) -> set[str]:
        items = (self.pipeline.batch_items() if hasattr(type(self.pipeline), 'batch_items')
                 else self.pipeline.queue.list_active())
        known = {item.codex_application_session_id for item in items
                 if item.codex_application_session_id}
        # A held job waits for the user and takes no slot, even while its kept session still runs.
        held = {item.codex_application_session_id for item in items if item.held}
        # Submitted jobs may have left the queue while their original owner is
        # still running closeout.
        if self._progress_snapshot:
            known.update(j['owner'] for j in self._progress_snapshot['jobs']
                         if j.get('owner'))
        from pipeline.claude_dispatch import active_claude_owners, is_claude_owner
        claude = {owner for owner in known if is_claude_owner(owner)}
        active = active_claude_owners(self.pipeline, claude) if claude else set()
        return active - held

    def _waiting_application_threads(self, active: set[str]) -> set[str]:
        from pipeline.claude_dispatch import is_claude_owner, waiting_claude_owners
        claude = {owner for owner in active if is_claude_owner(owner)}
        return waiting_claude_owners(self.pipeline, claude) if claude else set()

    def _schedule_applications(self) -> None:
        from pipeline.submission_reservation import reservations, occupies_slot
        submissions = reservations(self.pipeline.config.resume_root)
        pending = {row['job'] for row in submissions}
        from pipeline.claude_dispatch import is_claude_owner
        # A Claude Applier submits in its own session, which holds the slot while it works. Its blocked
        # confirmation waits on the user or a reconciliation, and holds none.
        reserved = {row['job'] for row in submissions if occupies_slot(row)
                    and not (row['state'] == 'blocked' and is_claude_owner(row['owner']))}
        try:
            external = self._active_application_threads()
            # An Applier idle on the user keeps its job and session but not a slot.
            waiting = self._waiting_application_threads(external)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.emit({"event": "application_capacity_unavailable", "error": str(exc)})
            self._busy_owners = None
            return
        busy = external - waiting
        self._busy_owners = busy
        if hasattr(type(self.pipeline), 'finish_submission'):
            for receipt in (self.pipeline.config.resume_root / '.pipeline' / 'submission-closeouts').glob('*.json'):
                data = json.loads(receipt.read_text())
                job_id = data['job_id']
                batch_ids = getattr(self.pipeline, 'batch_ids', None)
                if (data.get('complete') or (batch_ids is not None and job_id not in batch_ids)
                        or not self._recovery_allowed('submission_cleanup', job_id)
                        or job_id in self._application_futures or job_id in pending or data['thread_id'] in external
                        or self._paused(job_id)
                        or time.monotonic() - self._last_recovery.get(job_id, 0) < self.settings.recovery_interval
                        or len(self._application_futures) + len(busy) >= self.settings.application_workers):
                    continue
                self._last_recovery[job_id] = time.monotonic()
                self._application_futures[job_id] = ('submission_cleanup', self._applications.submit(self.pipeline.finish_submission, job_id))
        if self.progress_monitor and self._progress_snapshot:
            # Reuse the formal service; never click Submit for synchronization.
            for observed in self._progress_snapshot['jobs']:
                job_id = observed['job_id']
                if (observed['submitted'] and not observed['synced'] and not observed['errors']
                        and self._recovery_allowed('submission_cleanup', job_id)
                        and observed['owner'] not in external and job_id not in self._application_futures
                        and len(self._application_futures) + len(busy) < self.settings.application_workers
                        and time.monotonic() - self._last_recovery.get(job_id, 0) >= self.settings.recovery_interval):
                    self._last_recovery[job_id] = time.monotonic()
                    self._application_futures[job_id] = ('submission_cleanup', self._applications.submit(self.pipeline.finish_submission, job_id))
        items = (self.pipeline.batch_items() if hasattr(type(self.pipeline), "batch_items")
                 else self.pipeline.queue.list_active())
        # Count reservations and live sessions once.
        owned_threads = {item.codex_application_session_id for item in items
                         if item.job_id in self._application_futures}
        # A closeout whose owner has ended holds no slot: nobody is closing it,
        # and the blocker audit reports it (2026-10-15 user decision).
        occupied = len(self._application_futures) + len(busy - owned_threads)
        occupied += sum(i.job_id in reserved and not i.held and i.job_id not in self._application_futures and i.codex_application_session_id not in busy for i in items)
        pre_application = {ApplicationState.QUEUED, ApplicationState.DISPATCHING, ApplicationState.TAILORING, ApplicationState.TAILORING_PAUSED, ApplicationState.RESUME_READY, ApplicationState.DEFERRED}
        review_wait = {ApplicationState.READY_FOR_REVIEW, ApplicationState.AWAITING_APPROVAL}
        review_wait.add(ApplicationState.PLATFORM_WAIT)
        review_wait |= WAITING_ON_USER  # Idle waits keep their owner but not a slot.
        if self.progress_monitor:
            review_wait |= {ApplicationState.RETRY_LATER}  # Monitor disables auto retry; idle ones never move.
        # A job the monitor stops cannot progress; while no agent runs it, it holds no slot.
        stopped = (self._error_blocked(self._progress_snapshot) or set()) if self.progress_monitor and self._progress_snapshot else set()
        confirmed = {i.job_id for i in items if hasattr(type(self.pipeline), '_user_submission') and self.pipeline._user_submission(i.job_id)}
        # A Claude job holds a slot only while its Applier runs; a parked or ended one waits outside every slot.
        unfinished = sum(i.state not in pre_application and i.state not in TERMINAL_STATES and not i.held
                         and (i.job_id in self._application_futures or i.codex_application_session_id in busy
                              or (not is_claude_owner(i.codex_application_session_id)
                                  and i.state not in review_wait and i.job_id not in stopped and i.job_id not in confirmed
                                  and i.codex_application_session_id not in waiting))
                         for i in items)
        unfinished += sum(i.state in pre_application and i.job_id in self._application_futures for i in items)
        known_threads = {i.codex_application_session_id for i in items if i.state not in pre_application and i.state not in TERMINAL_STATES}
        unfinished += len(busy - known_threads - owned_threads)
        monotonic_now = time.monotonic()
        for item in items:
            if (self._paused(item.job_id) or item.job_id in self._application_futures or item.job_id in pending
                    or item.codex_application_session_id in external):
                continue
            if item.job_id in confirmed:
                if (monotonic_now - self._last_recovery.get(item.job_id, 0) >= self.settings.recovery_interval
                        and self._recovery_allowed('submission_cleanup', item.job_id)
                        and len(self._application_futures) + len(busy) < self.settings.application_workers):
                    self._last_recovery[item.job_id] = monotonic_now
                    self._application_futures[item.job_id] = ('submission_cleanup', self._applications.submit(self.pipeline.finish_submission, item.job_id))
                    occupied += 1  # Reserve the cleanup writer before scheduling another job.
                continue  # Confirmation only permits synchronization and cleanup.
            action: str | None = None
            call: Callable[[], Any] | None = None
            if hasattr(type(self.pipeline), "reconcile_applied") and self.pipeline.jobfeed.is_applied(item.job_id):
                if monotonic_now - self._last_recovery.get(item.job_id, 0.0) < self.settings.recovery_interval:
                    continue
                self._last_recovery[item.job_id] = monotonic_now
                action = "submission_reconciled"
                call = partial(self.pipeline.reconcile_applied, item.job_id)
            elif item.state == ApplicationState.RESUME_READY:
                reservations = sum(i.job_id in self._application_futures and i.state == ApplicationState.RESUME_READY for i in items)
                if hasattr(type(self.pipeline), 'can_start_application_for'):
                    if not self.pipeline.can_start_application_for(item.job_id, reservations):
                        continue
                elif hasattr(type(self.pipeline), 'can_start_application') and not self.pipeline.can_start_application(reservations):
                    continue
                action = "application_started"
                call = partial(self.pipeline.start_ready_application, item.job_id)
            elif item.state in {
                ApplicationState.APPLYING,
                ApplicationState.REVIEWING,
                ApplicationState.NEEDS_VERIFICATION,
                ApplicationState.SUBMITTING,
                ApplicationState.SUBMISSION_UNKNOWN,
            }:
                last = self._last_recovery.get(item.job_id, 0.0)
                if monotonic_now - last >= self.settings.recovery_interval:
                    self._last_recovery[item.job_id] = monotonic_now
                    action = "application_sync"
                    call = partial(self.pipeline.sync_claude_application, item.job_id)
            if not self._progress_allows(action, item.job_id):
                continue
            if action in RECOVERY_ACTIONS and not self._recovery_allowed(action, item.job_id):
                continue
            if action is None or call is None:
                continue
            if action == "application_started":
                if unfinished >= self.settings.application_workers:
                    continue
                unfinished += 1
            if action != "application_sync":
                if occupied >= self.settings.application_workers:
                    continue
                # An existing active session must not receive another writer or replacement.
                if item.codex_application_session_id in external:
                    continue
                occupied += 1
            elif len(self._application_futures) >= self.settings.application_workers:
                continue
            future = self._applications.submit(call)
            self._application_futures[item.job_id] = (action, future)
            self.emit({"event": action, "job_id": item.job_id})


def _print_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, default=str, separators=(",", ":")), flush=True)
