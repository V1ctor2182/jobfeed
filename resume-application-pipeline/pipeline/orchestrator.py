"""Deterministic coordinator for the resume application lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.cancellation import CancellationStore
from pipeline.config import PipelineConfig
from pipeline.jobfeed import Job, Jobfeed
from pipeline.lifecycle import ApplicationState
from pipeline.store import ApplicationQueue, QueueItem


@dataclass
class ApplicationExecution:
    """The scope application work runs under, and the gate that can cancel it."""

    execution_scope: str
    execution_gate: CancellationStore


class ApplicationPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        queue: ApplicationQueue | None = None,
        jobfeed: Jobfeed | None = None,
    ):
        self.config = config
        self.queue = queue or ApplicationQueue(config.jobfeed_db)
        self.jobfeed = jobfeed or Jobfeed(config.jobfeed_db, config.jobfeed_root)
        self.applications = ApplicationExecution(
            str(config.resume_root), CancellationStore(config.resume_root / '.pipeline' / 'execution-gates.sqlite3'))
        self.batch_limit = 4

    def batch_items(self):
        return self.queue.list_active()

    def initialize(self) -> None:
        self.queue.initialize()

    def enqueue(self, job_id: int) -> bool:
        self.jobfeed.get_job(job_id)
        if self.jobfeed.is_applied(job_id):
            return False
        return self.queue.enqueue(job_id)

    def prepare_existing_resumes(self):
        from pipeline.resume_catalog import local_catalog, prepare_application, verify_catalog
        catalog = getattr(self, 'resume_catalog', None) or local_catalog(self.config)
        verify_catalog(self.config.resume_root, catalog)
        scope = str(getattr(self, 'manifest_path', None) or self.applications.execution_scope)
        items = self.batch_items() if hasattr(self, 'batch_items') else self.queue.list_active()
        prepared = []
        for item in items:
            if item.state != ApplicationState.QUEUED or item.codex_application_session_id or item.held or self.queue.paused(item.job_id):
                continue
            folder = prepare_application(self.config, self.jobfeed.get_job(item.job_id), scope, catalog)
            self.queue.transition(item.job_id, ApplicationState.RESUME_READY,
                                  application_folder_relpath=str(folder.relative_to(self.config.applications_root)))
            prepared.append(item.job_id)
        return prepared

    def process_completions(self) -> list[int]:
        return self.prepare_existing_resumes()

    def _user_submission(self, job_id: int) -> dict:
        from pipeline.submission_state import read_user_submission
        item = self.queue.get(job_id)
        folder = item.application_folder_relpath if item else None
        if not folder:
            pointer = self.config.resume_root / '.pipeline/submission-closeouts' / f'{job_id}.json'
            try:
                data = json.loads(pointer.read_text())
                folder = data.get('folder') if data.get('job_id') == job_id else None
            except (OSError, ValueError):
                return {}
        if not folder:
            return {}
        root = self.config.applications_root.resolve()
        path = (root / folder).resolve()
        return read_user_submission(path, job_id) if path.is_relative_to(root) else {}

    def _assert_not_user_submitted(self, job_id: int) -> None:
        if self._user_submission(job_id):
            raise ValueError('用户确认已提交，禁止重投或填写；仅允许正式同步和保留原页核验')

    def reconcile_applied(self, job_id: int, submitted_item: QueueItem | None = None) -> bool:
        """Confirm formal submission without closing or resuming the original form."""
        if not self.jobfeed.is_applied(job_id):
            return False
        advice = self.queue.current_advice(job_id)
        if advice and advice['status'] != 'critical':
            # Successful application makes old form questions obsolete. Keep any
            # explicit critical investigation for the control session to resolve.
            self.queue.update_advice(advice['id'], advice['revision'], status='resolved')
        pointer = self.config.resume_root / '.pipeline' / 'submission-closeouts' / f'{job_id}.json'
        item = self.queue.get(job_id) or submitted_item
        if item is None and pointer.exists():
            saved = json.loads(pointer.read_text())
            item = QueueItem(job_id=job_id, state=ApplicationState.ALREADY_APPLIED,
                application_folder_relpath=saved['folder'], codex_application_session_id=saved['thread_id'])
        if item is None:
            return True
        if not item.application_folder_relpath or not item.codex_application_session_id:
            raise ValueError('applied application is missing original folder/session for closeout')
        from jobfeed.submission_evidence import EvidenceValidationError
        try:
            self._confirmation(job_id, item)
        except EvidenceValidationError:
            return True
        receipt = self._folder(item) / 'submission-receipt.json'
        data = json.loads(receipt.read_text()) if receipt.exists() else {}
        data.update(job_id=job_id, jobfeed_synced=True)
        if self._user_submission(job_id):
            data['submission_source'] = 'user_confirmation'
        data.update(close_pending=False, tab_policy='keep_open')
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pending = json.loads(pointer.read_text()) if pointer.exists() else {}
        pending.update({'job_id': job_id, 'folder': item.application_folder_relpath,
                        'thread_id': item.codex_application_session_id, 'complete': True,
                        'tab_policy': 'keep_open'})
        pointer.write_text(json.dumps(pending, ensure_ascii=False, indent=2) + '\n')
        data.pop('close_error', None)
        receipt.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        if self.queue.get(job_id):
            self.queue.transition(job_id, ApplicationState.ALREADY_APPLIED)

        return True

    def _confirmation(self, job_id: int, item: QueueItem):
        from jobfeed.submission_evidence import read_confirmation
        job = self.jobfeed.get_job(job_id)
        return read_confirmation(self._folder(item), job_id, job.url, company=job.company)

    def confirmation_available(self, job_id: int) -> bool:
        from jobfeed.submission_evidence import EvidenceValidationError
        item = self.queue.get(job_id)
        if item is None:
            path = self.config.resume_root / '.pipeline' / 'submission-closeouts' / f'{job_id}.json'
            try:
                saved = json.loads(path.read_text())
                if saved.get('job_id') != job_id or not saved.get('folder'):
                    return False
                item = QueueItem(job_id=job_id, state=ApplicationState.ALREADY_APPLIED,
                                 application_folder_relpath=saved['folder'])
            except (OSError, ValueError):
                return False
        if not item.application_folder_relpath:
            return False  # A job that never started has nowhere to hold a confirmation.
        try:
            self._confirmation(job_id, item)
        except (EvidenceValidationError, OSError, KeyError):
            return False
        return True

    def _submission_unverified(self, job_id: int, reason: str) -> dict[str, Any]:
        item = self.queue.get(job_id)
        from pipeline.lifecycle import TERMINAL_STATES
        if item and item.state not in TERMINAL_STATES and item.state != ApplicationState.SUBMISSION_UNKNOWN:
            self.queue.transition(job_id, ApplicationState.SUBMISSION_UNKNOWN)
        return {'status': 'submission_unknown', 'job_id': job_id,
                'reason': reason, 'next': 'Save confirmation evidence for this job; never submit again'}

    def finish_submission(self, job_id: int) -> dict[str, Any]:
        """Synchronize only artifact-backed confirmation, without submitting again."""
        item = self.queue.get(job_id)
        if item is None:
            pointer = self.config.resume_root / '.pipeline' / 'submission-closeouts' / f'{job_id}.json'
            saved = json.loads(pointer.read_text()) if pointer.exists() else {}
            if saved.get('job_id') == job_id and saved.get('folder'):
                item = QueueItem(job_id=job_id, state=ApplicationState.ALREADY_APPLIED,
                    application_folder_relpath=saved['folder'], codex_application_session_id=saved.get('thread_id'))
        if item is None:
            return self._submission_unverified(job_id, 'confirmation_artifact_required')
        from jobfeed.submission_evidence import EvidenceValidationError
        try:
            self._confirmation(job_id, item)
        except EvidenceValidationError as exc:
            return self._submission_unverified(job_id, str(exc))
        if not self.reconcile_applied(job_id):
            self.jobfeed.record_applied(job_id, self._folder(item))
            self.reconcile_applied(job_id, submitted_item=item)
        pointer = self.config.resume_root / '.pipeline' / 'submission-closeouts' / f'{job_id}.json'
        if not pointer.exists():
            return {'status':'jobfeed_synced', 'job_id':job_id, 'cleanup':'unknown'}
        saved = json.loads(pointer.read_text())
        return {'status':'submission_completed' if saved['complete'] else 'submission_cleanup_pending',
                'job_id':job_id, 'jobfeed_synced':True, 'tab_policy':'keep_open',
                'tabs_closed':bool(json.loads((self._folder(item) / 'submission-receipt.json').read_text()).get('tabs_closed'))}

    def start_ready_application(self, job_id: int) -> dict[str, Any]:
        self._assert_not_paused(job_id)
        item = self._require(job_id, ApplicationState.RESUME_READY)
        job = self.jobfeed.get_job(job_id)
        folder = self._folder(item)
        return self._start_application(item, job, folder)

    def _start_application(self, item: QueueItem, job: Job, folder: Path) -> dict[str, Any]:
        self._assert_not_user_submitted(item.job_id)
        from pipeline.claude_dispatch import start_claude_application
        return start_claude_application(self, item, job, folder)

    def sync_claude_application(self, job_id: int) -> dict[str, Any]:
        from pipeline.claude_dispatch import sync_claude_application
        return sync_claude_application(self, job_id)

    def cancel(self, job_id: int) -> None:
        item = self.queue.get(job_id)
        if item is None:
            raise KeyError(job_id)
        scope = str(getattr(self, 'manifest_path', None) or getattr(self.applications, 'execution_scope', None) or self.config.resume_root)
        gate = CancellationStore(self.config.resume_root / '.pipeline' / 'execution-gates.sqlite3')
        if item.codex_application_session_id:
            gate.bind(scope, job_id, item.codex_application_session_id)
        cancellation = gate.cancel(scope, job_id, f'controller:cancel:{job_id}', reason='stopped_by_user')
        self.queue.transition(job_id, ApplicationState.CANCELLED)
        self._cancel_decision_work(job_id, scope, {'source': 'controller_cancel', 'job_id': job_id},
                                   cancellation['received_at'])

    def _cancel_decision_work(self, job_id, scope, evidence, received_at):
        """Cancel the job's blocker-ledger work; sent or unknown operations stay for reconciliation."""
        import sqlite3
        from pipeline.decision_queue import DecisionQueue
        path = self.config.resume_root / '.pipeline' / 'blockers.sqlite3'
        controller = 'controller'
        if path.exists():
            with sqlite3.connect(path) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='dq_scopes'").fetchone():
                    row = db.execute('SELECT controller FROM dq_scopes WHERE scope=?', (scope,)).fetchone()
                    if row:
                        controller = row[0]
        ledger = DecisionQueue(path, scope=scope, controller=controller)
        try:
            ledger.cancel_job(job_id, 'cancelled', evidence, received_at)
        finally:
            ledger.db.close()

    def _assert_not_paused(self, job_id: int | None = None) -> None:
        if self.queue.paused(job_id):
            raise ValueError("critical 暂停中；需要明确恢复指示")
        if job_id is not None:
            self._assert_not_user_submitted(job_id)

    def _require(self, job_id: int, state: ApplicationState) -> QueueItem:
        item = self.queue.get(job_id)
        if item is None or item.state != state:
            actual = None if item is None else item.state
            raise ValueError(f"job {job_id} must be {state}; found {actual}")
        return item

    def _folder(self, item: QueueItem) -> Path:
        if not item.application_folder_relpath:
            raise ValueError(f"job {item.job_id} has no application folder")
        original = self.config.applications_root / item.application_folder_relpath
        for path in [original, *original.parents]:
            if path.is_symlink():
                raise ValueError('application folder cannot contain symlinks')
            if path == self.config.applications_root:
                break
        folder = original.resolve()
        folder.relative_to(self.config.applications_root.resolve())
        return folder
