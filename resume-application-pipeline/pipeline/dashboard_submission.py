"""Local dashboard confirmation dispatches one existing owner, never a worker."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import UUID, uuid5

from pipeline.answer_bank import AnswerBank, _date, applier_decided
from pipeline.application_rerun import RerunError, read_thread, host_turn_state, send_existing, stop_snapshot, worker_stopped
from pipeline.cancellation import CancellationStore
from pipeline.config import PipelineConfig
from pipeline.codex_app_server import AppServerError, AppServerTimeout
from pipeline.resume_catalog import selected_materials
from pipeline.store import ApplicationQueue
from pipeline.submission_reservation import admission_lock, reservations, occupies_slot

ELIGIBLE = {'Applying', 'Needs Input', 'Needs Verification', 'Resume Ready', 'Retry Later',
            'Ready for Review', 'Reviewing', 'Awaiting Approval', 'Submit Authorized'}
OPEN = {'preparing', 'unknown', 'queued', 'running'}
EVIDENCE = ('submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json',
            'submission-attempt.json', 'submission-result.json')


DASHBOARD_ORIGIN = {'kind': 'dashboard', 'actor': 'local-user'}
DISCORD_REQUESTS = UUID('6f1c2b8e-4a53-4c1e-9d0a-5b7e2f3c8a41')
AUTO_REQUESTS = UUID('2d8e4f61-93b7-4c0a-a5e2-7f1b6c9d3e08')
CONFIRMED_BY = {'dashboard': 'The user confirmed Submit in the local Dashboard', 'discord': 'The user confirmed Submit in Discord',
                'auto': "The user's auto submit mode confirmed Submit"}


class SubmissionError(ValueError):
    pass


class SubmissionTransportUnavailable(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


class DashboardSubmissions:
    def __init__(self, config):
        self.config = config
        self.root = config.resume_root
        self.path = self.root / '.pipeline/dashboard-submissions.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, job INTEGER NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
                preview TEXT NOT NULL, pinned TEXT, prompt TEXT, baseline TEXT, turn_id TEXT,
                expires_at TEXT, receipt TEXT, reason TEXT, created_at TEXT NOT NULL)''')
            if 'mode' not in {row[1] for row in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN mode TEXT')
            if 'terminal_turn' not in {row[1] for row in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN terminal_turn TEXT')
            if 'last_checked_at' not in {row[1] for row in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN last_checked_at TEXT')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open_submission ON requests(job) WHERE state IN ('preparing','unknown','queued','running')")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            yield db

    @contextmanager
    def locked(self):
        with admission_lock(self.root):
            yield

    def row(self, request_id):
        if str(UUID(request_id)) != request_id:
            raise SubmissionError('A canonical UUID request ID is required.')
        with self.db() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def resolve_scope(self, *, scope=None, batch_id=None):
        if batch_id is not None:
            if Path(batch_id).name != batch_id or batch_id in {'.', '..'}:
                raise SubmissionError('Invalid batch ID.')
            monitor = json.loads((self.root / 'monitor-output' / batch_id / 'monitor.json').read_text())
            scope = monitor['source']
        path = Path(scope)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve(strict=True)
        if not path.is_relative_to((self.root / '.pipeline/batches').resolve()):
            raise SubmissionError('Manifest is outside the batch directory.')
        return str(path)

    def _preview(self, scope, job_id, *, active=False, attempt_request=None, attempt_turn=None):
        if type(job_id) is not int or job_id < 1:
            raise SubmissionError('Invalid job ID.')
        scope = self.resolve_scope(scope=scope)
        manifest = json.loads(Path(scope).read_text())
        candidate = next((c for c in manifest['ordered_candidates'] if c['id'] == job_id), None)
        if not candidate:
            raise SubmissionError('Job is outside this batch.')
        queue = ApplicationQueue(self.config.jobfeed_db)
        with sqlite3.connect(self.config.jobfeed_db) as db:
            if db.execute('SELECT 1 FROM applied WHERE job_id=?', (job_id,)).fetchone():
                raise SubmissionError('This application is already submitted.')
        if (self.root / '.pipeline/submission-closeouts' / f'{job_id}.json').exists():
            raise SubmissionError('This application has submission closeout evidence. Never resubmit.')
        item = queue.get(job_id)
        if not item or not item.codex_application_session_id or not item.application_folder_relpath:
            raise SubmissionError('An existing original owner and application folder are required.')
        owner = item.codex_application_session_id
        folder = (self.config.applications_root / item.application_folder_relpath).resolve(strict=True)
        if not folder.is_relative_to(self.config.applications_root.resolve()):
            raise SubmissionError('Application folder escapes runtime.')
        if item.state.value not in ELIGIBLE | ({'Submitting'} if active else set()):
            raise SubmissionError('Application state requires reconciliation or is already terminal.')
        evidence = list(EVIDENCE)
        attempt = folder / 'submission-attempt.json'
        if active and attempt_request and attempt_turn and attempt.exists():
            marker = json.loads(attempt.read_text())
            if marker.get('dashboard_request_id') == attempt_request and marker.get('owner_turn_id') == attempt_turn:
                evidence.remove('submission-attempt.json')
        if any((folder / name).exists() for name in evidence) or (self.root / '.pipeline/submission-closeouts' / f'{job_id}.json').exists():
            raise SubmissionError('Submission evidence or an earlier attempt requires reconciliation. Never resubmit.')
        gate = CancellationStore(self.root / '.pipeline/execution-gates.sqlite3')
        with gate._db() as db:
            binding = db.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (owner,)).fetchone()
            if not binding or tuple(binding) != (scope, job_id):
                raise SubmissionError('Original owner binding changed.')
            stops = stop_snapshot(db, scope, job_id)
            if db.execute("SELECT 1 FROM application_reruns WHERE job_id=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND turn_id IS NULL AND json_extract(receipt,'$.delivery_started')=1))", (job_id,)).fetchone():
                raise SubmissionError('An earlier owner continuation has an unresolved delivery. Reconcile it first.')
        deliveries = self.root / '.pipeline/desktop-deliveries.sqlite3'
        if deliveries.exists():
            with sqlite3.connect(deliveries) as db:
                if db.execute("SELECT 1 FROM deliveries WHERE job=? AND state IN ('pending','unknown','sent')", (job_id,)).fetchone():
                    raise SubmissionError('An earlier Desktop delivery is pending or unknown. Reconcile it first.')
        from pipeline.claude_dispatch import is_claude_owner
        if is_claude_owner(owner):
            # Claude batches have no Controller; the manifest's own membership and stop flags stand in for it.
            controller, member = (None, manifest.get('stopped_by_user') is not True), ('active',)
            handoff = {'manifest': scope, 'status': 'stopped_by_user' if manifest.get('stopped_by_user') else 'running'}
        else:
            with sqlite3.connect(self.root / '.pipeline/blockers.sqlite3') as db:
                controller = db.execute('SELECT controller,active FROM dq_scopes WHERE scope=?', (scope,)).fetchone()
                member = db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (scope, str(job_id))).fetchone()
                if not controller or controller[0] != manifest['controller_thread_id'] or not member:
                    raise SubmissionError('Controller or batch membership changed.')
            handoff_path = Path(scope).with_name(Path(scope).stem + '-handoff.json')
            handoff = json.loads(handoff_path.read_text())
            if handoff.get('manifest') != scope:
                raise SubmissionError('Batch handoff changed.')
        bank = AnswerBank(self.root).read()
        answers = []
        warnings = []
        for entry in bank['entries']:
            version = entry['versions'][-1]
            # A merged answer's wordings now belong to the answer it was merged into, which shows instead.
            if version.get('merged_into'):
                continue
            answer_scope = version['scope']
            # The answers this job's Applier decided itself under auto submit mode show too, so the user sees them before Submit.
            if (answer_scope.get('kind') == 'job' and str(answer_scope.get('job_id')) == str(job_id) and answer_scope.get('company', '').casefold() == candidate['company'].casefold()
                    or applier_decided(version, job_id)):
                if version.get('valid_until') and _date(version['valid_until']) <= now():
                    raise SubmissionError('An answer has expired. Edit and review a new version first.')
                if version['state'] not in {'approved', 'pending_review'}:
                    raise SubmissionError('A revoked answer needs a new reviewed version.')
                answers.append({'entry_id': entry['id'], **{k: version[k] for k in ('version','question','answer','state','scope','valid_until')}})
        if any(a['state'] != 'approved' for a in answers):
            warnings.append('Confirming approves the exact pending answer versions displayed here.')
        paused = queue.paused(job_id)
        if paused:
            warnings.append('This job has a workflow review pause. Confirm overrides this pause for this owner turn only.')
        if not controller[1] or stops['scope'] or stops['job'] or handoff.get('status') == 'stopped_by_user':
            warnings.append('The batch is stopped. Confirm permits this job only and keeps the worker stopped.')
        if member[0] != 'active':
            warnings.append('This job is held in the workflow. Confirm permits this job only.')
        try:
            selected = selected_materials(folder, job_id=job_id, scope=scope, owner=owner)
        except FileNotFoundError:
            raise SubmissionError('The Applier has not chosen a resume or filled this form yet. Submit is available once the row says Ready for review.') from None
        return {'eligible': True, 'reason': None, 'scope': scope, 'job_id': job_id, 'owner': owner,
                'company': candidate['company'], 'title': candidate.get('title',''), 'state': item.state.value,
                'controller': controller[0], 'project': manifest.get('desktop_project_root', str(self.root)),
                'answers': answers, 'bank_revision': bank['revision'], 'warnings': warnings,
                'selected_resume': selected,
                'stop_snapshot': stops, 'workflow': {'paused': paused, 'scope_active': controller[1],
                    'review': queue.current_advice(job_id), 'membership': member[0], 'handoff': handoff, 'manifest': manifest},
                'submit_authorization': queue.submit_authorization(job_id)}

    def preview(self, *, job_id, scope=None, batch_id=None):
        try:
            return self._preview(self.resolve_scope(scope=scope, batch_id=batch_id), job_id)
        except (OSError, ValueError, KeyError, TypeError, StopIteration, sqlite3.Error) as exc:
            return {'eligible': False, 'reason': str(exc), 'job_id': job_id}

    def _thread(self, preview, *, idle=False):
        try:
            thread = read_thread(preview['owner'])
        except (OSError, AppServerTimeout) as exc:
            raise SubmissionTransportUnavailable('Application agent connection is temporarily unavailable.') from exc
        if thread.get('id') != preview['owner'] or thread.get('originator') != 'Codex Desktop' or thread.get('cwd') != preview['project'] or thread.get('archived'):
            raise SubmissionError('The original Desktop owner or project changed.')
        turns = thread.get('turns', [])
        if not turns:
            raise SubmissionError('Original owner lifecycle unavailable.')
        if idle and host_turn_state(thread, turns[-1]['id'], preview['project']) not in {'completed','interrupted'}:
            raise SubmissionError('Original owner is busy or waiting for approval.')
        return thread

    def confirm(self, *, request_id, expected_preview, confirmed):
        if confirmed is not True:
            raise SubmissionError('Explicit local dashboard confirmation is required.')
        return self._confirm(request_id, expected_preview, DASHBOARD_ORIGIN)

    def confirm_discord(self, *, job_id, scope, message_id, sender_id):
        """Confirm Submit from the user's Discord reply, through the same checks as the Dashboard.

        Only a clean Claude draft qualifies: anything the Dashboard would warn about needs the Dashboard.
        """
        from pipeline.claude_dispatch import is_claude_owner
        # The same Discord message always maps to the same request, so a repeated sync cannot confirm twice.
        request_id = str(uuid5(DISCORD_REQUESTS, message_id))
        if (old := self.row(request_id)) is not None:
            return self._public(old)
        preview = self._preview(self.resolve_scope(scope=scope), job_id)
        if not is_claude_owner(preview['owner']):
            raise SubmissionError('Only a Claude Applier can be confirmed from Discord.')
        if preview['warnings']:
            raise SubmissionError('This job needs the Dashboard confirmation. ' + ' '.join(preview['warnings']))
        return self._confirm(request_id, preview, {'kind': 'discord', 'actor': f'discord:{sender_id}', 'message_id': message_id})

    def confirm_auto(self, *, job_id, scope):
        """Confirm Submit for a Claude Applier's draft that is ready for review, under the auto submit mode the user turned on.

        It runs the same checks as the Dashboard, and each Submit attempt of an Applier gets at most one auto confirmation.
        Returns None and records nothing while the mode is off or the job still needs the user: an open question or blocker,
        an answer waiting for review, any warning the Dashboard would show, or an Applier that is stopped, missing or draining.
        """
        from pipeline.applier_discord import REVIEW_STEP
        from pipeline.applier_runtime import RETRYABLE_SUBMIT, ApplierRuntime
        from pipeline.claude_applier import WAKEABLE, ClaudeApplierLauncher
        with self.locked():
            runtime = ApplierRuntime(self.root)
            mode = runtime.auto_submit()
            job = next((j for j in runtime.jobs() if j['job_id'] == job_id), None)
            if not mode['enabled'] or job is None:
                return None
            submit = runtime.submit_state(job_id)
            request_id = str(uuid5(AUTO_REQUESTS, f"{job['owner']}:{submit['attempts'] if submit else 0}"))
            if (old := self.row(request_id)) is not None:
                return self._public(old)
            if (job['last_progress'] != REVIEW_STEP or runtime.control('draining') or runtime.skip(job_id) or runtime.unused_submit_auth(job_id)
                    or (submit is not None and (submit['state'] not in RETRYABLE_SUBMIT or job['last_progress_at'] < submit['outcome_at']))
                    or any(r['job_id'] == job_id and r['state'] in {'open', 'escalated'} for r in runtime.requests())):
                return None
            preview = self.preview(job_id=job_id, scope=scope)
            if (not preview['eligible'] or preview['owner'] != job['owner'] or preview['warnings']
                    or any(answer['state'] != 'approved' for answer in preview['answers'])):
                return None
            launcher = ClaudeApplierLauncher(self.root)
            launch_id = launcher.launch_for(job_id)
            # A stopped Applier was stopped on purpose, and one the CLI no longer reports cannot be woken.
            if launch_id is None or launcher.reconcile(launch_id)['state'] not in WAKEABLE:
                return None
            return self._confirm(request_id, preview, {'kind': 'auto', 'actor': 'auto-submit',
                                                       'enabled_at': mode['enabled_at'], 'enabled_by': mode['enabled_by']})

    def _confirm(self, request_id, expected_preview, origin):
        self.row(request_id)
        with self.locked():
            old = self.row(request_id)
            if old:
                if json.loads(old['preview']) != expected_preview:
                    raise SubmissionError('Request ID belongs to another confirmation.')
                return self._public(old)
            # A request marked not submitted already recorded that its attempt sent nothing.
            with self.db() as db:
                previous = db.execute("SELECT id FROM requests WHERE job=? AND prompt IS NOT NULL AND state!='not_submitted' ORDER BY created_at DESC LIMIT 1",
                                      (expected_preview['job_id'],)).fetchone()
            if previous:
                if not self._claude_retry_allowed(expected_preview['job_id']):
                    raise SubmissionError('An earlier submission delivery must be reconciled. Never resend with a new request ID.')
                with self.db() as db:
                    db.execute("UPDATE requests SET state='not_submitted',reason=? WHERE job=? AND mode='claude' AND state IN ('queued','running')",
                               ('The Applier recorded that this attempt sent nothing.', expected_preview['job_id']))
            current = self._preview(expected_preview['scope'], expected_preview['job_id'])
            if current != expected_preview:
                raise SubmissionError('Preview changed. Review current warnings and exact answers again.')
            with self.db() as db:
                db.execute('INSERT INTO requests(id,job,owner,state,preview,created_at,expires_at,reason) VALUES(?,?,?,?,?,?,?,?)',
                    (request_id, current['job_id'], current['owner'], 'preparing', json.dumps(current),
                     now().isoformat(), None,
                     'Confirmation saved. Waiting for your application agent to become available.'))
            from pipeline.claude_dispatch import is_claude_owner
            if is_claude_owner(current['owner']):
                return self._advance_claude(request_id, origin)
            if origin is not DASHBOARD_ORIGIN:
                raise SubmissionError('Only a Claude Applier can be confirmed from Discord or by auto submit mode.')
            return self._advance_stopped(request_id)

    def settle_requeued(self, job_id, request_ids, reason):
        """Mark a requeued job's requests as not submitted. The requeue command checked that each approval sent nothing:
        its attempt ended so, or request_submit never spent it."""
        with self.db() as db:
            db.executemany("UPDATE requests SET state='not_submitted',reason=? WHERE id=? AND job=? AND state IN ('blocked','turn_ended','preparing','unknown','queued','running')",
                           [(f'Nothing was sent with this approval. The job went back to the queue: {reason}', request_id, job_id) for request_id in request_ids])

    def _claude_retry_allowed(self, job_id):
        """A Claude Applier's last authorized attempt is reconciled once it is recorded as not submitted."""
        from pipeline.applier_runtime import RETRYABLE_SUBMIT, ApplierRuntime
        from pipeline.claude_dispatch import is_claude_owner
        item = ApplicationQueue(self.config.jobfeed_db).get(job_id)
        if item is None or not is_claude_owner(item.codex_application_session_id):
            return False
        runtime = ApplierRuntime(self.root)
        state = runtime.submit_state(job_id)
        return state is not None and state['state'] in RETRYABLE_SUBMIT and not runtime.unused_submit_auth(job_id)

    def _advance_claude(self, request_id, origin):
        """Record the confirmed Submit for a Claude Applier and wake it if it ended its turn. Nothing is sent to Codex."""
        from pipeline.applier_runtime import ApplierRuntime
        from pipeline.claude_applier import ClaudeApplierLauncher
        row = self.row(request_id)
        try:
            expected = json.loads(row['preview'])
            current = self._preview(expected['scope'], expected['job_id'])
            if dict(current, state=expected['state']) != expected:
                raise SubmissionError('Preview changed before delivery. Review current warnings and exact answers again.')
            bank, revision = AnswerBank(self.root), current['bank_revision']
            for answer in current['answers']:
                if answer['state'] == 'pending_review':
                    revision = bank.mutate({'action':'approve', 'entry_id':answer['entry_id'],
                        'expected_version':answer['version'], 'expected_revision':revision}, dashboard=True)['revision']
            source = {**origin, 'event_id':request_id, 'confirmed_at':now().isoformat(),
                      'controller_thread_id':f"claude:{current['owner']}", 'owner':current['owner']}
            ApplicationQueue(self.config.jobfeed_db).record_dashboard_submit_authorization(current['job_id'], source, dashboard=True)
            pinned = self._preview(current['scope'], current['job_id'])
            authorization = pinned['submit_authorization']
            if not authorization or not authorization['authorized'] or authorization.get('source', {}).get('event_id') != request_id:
                raise SubmissionError('Submission authorization changed during confirmation.')
            if pinned['answers'] != [dict(a, state='approved') for a in current['answers']] or pinned['bank_revision'] != revision:
                raise SubmissionError('Answers changed during confirmation. No work was delivered.')
            runtime = ApplierRuntime(self.root)
            runtime.authorize_submit(current['job_id'], source_kind=origin['kind'], event_id=request_id)
            launcher = ClaudeApplierLauncher(self.root)
            launch_id = launcher.launch_for(current['job_id'])
            message = (f'{CONFIRMED_BY[origin["kind"]]} for job {current["job_id"]} (request {request_id}). '
                       'Call tabs_context_mcp and find your registered tab. Read every field back, then call request_submit and follow its answer exactly.')
            # A busy Applier keeps the message queued; the supervisor delivers it once the Applier is idle.
            message_id = f'dashboard-submit-{request_id}'
            runtime.queue_message(current['job_id'], message, message_id)
            woken = 'queued'
            if launch_id is not None and runtime.claim_message(message_id):
                woken = launcher.wake(launch_id, message)
                if woken in {'messaged', 'restarted'}:
                    runtime.message_delivered(message_id, woken)
                else:
                    runtime.release_message(message_id)
            reason = {'stopped': 'Authorized. The Applier is stopped; restart it to continue to Submit.',
                      'draining': 'Authorized. Claude Appliers are draining; nothing was woken.'}.get(woken)
            with self.db() as db:
                db.execute("UPDATE requests SET state='queued',pinned=?,prompt=?,receipt=?,mode='claude',reason=? WHERE id=? AND state='preparing'",
                           (json.dumps(pinned), message, json.dumps({'claude_authorized': True, 'woken': woken}), reason, request_id))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
            self._withdraw_claude(request_id, str(exc))
        return self._public(self.row(request_id))

    def _withdraw_claude(self, request_id, reason):
        """Block a Claude confirmation, leaving nothing that a later wake or request_submit could use."""
        from pipeline.applier_runtime import ApplierRuntime
        row = self.row(request_id)
        runtime = ApplierRuntime(self.root)
        runtime.revoke_submit_auth(row['job'], request_id)
        runtime.cancel_message(f'dashboard-submit-{request_id}')
        self._restore_unsent_authorization(row)
        with self.db() as db:
            db.execute("UPDATE requests SET state='blocked',reason=? WHERE id=? AND state='preparing'", (reason, request_id))

    def _advance_stopped(self, request_id):
        from pipeline.application_rerun import RerunError
        with self.locked():
            try:
                with worker_stopped(self.root):
                    return self._advance(request_id, mode='stopped')
            except RerunError as exc:
                if str(exc) != 'The application worker is still active or draining.':
                    raise
                return self._public(self.row(request_id))

    def _stopped_capacity(self, current, thread):
        manifest = current['workflow']['manifest']
        selected = {int(candidate['id']) for candidate in manifest['ordered_candidates']}
        owners = {item.codex_application_session_id for item in ApplicationQueue(self.config.jobfeed_db).list_active()
                  if item.job_id in selected and item.codex_application_session_id}
        active = set()
        for owner in owners:
            observed = thread if owner == current['owner'] else self._thread(dict(current, owner=owner))
            if host_turn_state(observed, observed['turns'][-1]['id'], current['project']) == 'active':
                active.add(owner)
        occupied = {row['owner'] for row in reservations(self.root) if occupies_slot(row)}
        return len(active | occupied) < int(manifest['max_parallel'])

    def _restore_unsent_authorization(self, row):
        if row['prompt'] is not None:
            return
        expected = json.loads(row['preview'])
        with sqlite3.connect(self.config.jobfeed_db) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT submit_authorization FROM application_queue WHERE job_id=?', (row['job'],)).fetchone()
            if current and current[0] and json.loads(current[0]).get('source', {}).get('event_id') == row['id']:
                previous = expected['submit_authorization']
                db.execute('UPDATE application_queue SET submit_authorization=? WHERE job_id=? AND submit_authorization=?',
                           (json.dumps(previous) if previous else None, row['job'], current[0]))

    def _advance(self, request_id, *, mode):
        row = self.row(request_id)
        if row['state'] != 'preparing':
            return self._public(row)
        from pipeline.claude_dispatch import is_claude_owner
        if is_claude_owner(row['owner']):
            # A Claude confirmation settles inside _confirm under this same lock, so one still preparing was interrupted.
            self._withdraw_claude(request_id, 'The confirmation was interrupted before it finished. Its approval and wake were '
                                              'withdrawn. Confirm Submit again.')
            return self._public(self.row(request_id))
        try:
            expected = json.loads(row['preview'])
            current = self._preview(expected['scope'], expected['job_id'])
            comparable = dict(current, state=expected['state'])
            if comparable != expected:
                raise SubmissionError('Preview changed before delivery. Review current warnings and exact answers again.')
            thread = self._thread(current)
            if host_turn_state(thread, thread['turns'][-1]['id'], current['project']) not in {'completed','interrupted'}:
                with self.db() as db:
                    db.execute("UPDATE requests SET reason=? WHERE id=?", ('Confirmation saved. Your application agent is finishing its current task.', request_id))
                return self._public(self.row(request_id))
            if mode == 'stopped' and not self._stopped_capacity(current, thread):
                with self.db() as db:
                    db.execute('UPDATE requests SET reason=? WHERE id=?', ('Confirmation saved. Waiting for an application slot.', request_id))
                return self._public(self.row(request_id))
            with self.db() as db:
                db.execute('UPDATE requests SET baseline=?,mode=?,reason=NULL WHERE id=?',
                           (json.dumps([t['id'] for t in thread['turns']]), mode, request_id))
            bank = AnswerBank(self.root)
            revision = current['bank_revision']
            for answer in current['answers']:
                if answer['state'] == 'pending_review':
                    updated = bank.mutate({'action':'approve', 'entry_id':answer['entry_id'],
                        'expected_version':answer['version'], 'expected_revision':revision}, dashboard=True)
                    revision = updated['revision']
            source = {'kind':'dashboard', 'actor':'local-user', 'event_id':request_id,
                      'confirmed_at':now().isoformat(), 'controller_thread_id':current['controller'], 'owner':current['owner']}
            ApplicationQueue(self.config.jobfeed_db).record_dashboard_submit_authorization(current['job_id'], source, dashboard=True)
            pinned = self._preview(current['scope'], current['job_id'])
            authorization = pinned['submit_authorization']
            if not authorization or not authorization['authorized'] or authorization.get('source', {}).get('event_id') != request_id:
                raise SubmissionError('Submission authorization changed during confirmation.')
            expected_answers = [dict(a, state='approved') for a in current['answers']]
            if pinned['answers'] != expected_answers or pinned['bank_revision'] != revision:
                raise SubmissionError('Answers changed during confirmation. No work was delivered.')
            # All fields outside the explicitly confirmed mutations must remain exact.
            for key in current.keys() - {'answers','bank_revision','warnings','submit_authorization'}:
                if current[key] != pinned[key]:
                    raise SubmissionError('Application changed during confirmation. No work was delivered.')
            command = f'{self.root}/.venv/bin/python -m pipeline.dashboard_submission --root {self.root} owner-status'
            submit_step = (f'Record a durable submission-attempt.json immediately before the single Submit action, including dashboard_request_id "{request_id}" and owner_turn_id YOUR_ACTUAL_TURN_ID. ')
            prompt = (f'[dashboard-submit:{request_id}]\nThe user explicitly confirmed Submit in the local dashboard for job {current["job_id"]} only. '
                f'Continue as the existing owner of {current["scope"]}. Keep its original form and exact selected resume. '
                f'Confirmation approves only the exact answer versions in request {request_id}; do not generate new prose or facts. '
                'It overrides the displayed workflow holds for this job only. Do not restart a worker, create a chat, or apply elsewhere. '
                'Do not bypass browser security, CAPTCHA, account verification, or tool approval. '
                'Before any browser mutation, wait for the dashboard request to become running and its exact-turn release to validate. '
                f'The existing worker, dashboard, or owner-status command reconciles the already dispatched exact turn and establishes that release without sending again. Read the request using {command} with JSON request_id on stdin. '
                'Then re-read the preserved form, apply only its exact approved answers as needed, verify the selected resume with '
                '--submission-release --turn-id YOUR_ACTUAL_TURN_ID, and perform fresh pre-submit checks. '
                'Inspect existing submission evidence first. If any earlier submit attempt or uncertain result exists, reconcile it and never click again. '
                f'{submit_step}After clicking, only inspect and save outcome evidence; '
                'never click Submit a second time. Save a genuine submission-receipt.json only on confirmed success. '
                'Report uncertainty honestly and keep the page for manual verification.')
            with self.db() as db:
                changed = db.execute("UPDATE requests SET state='unknown',pinned=?,prompt=?,receipt=? WHERE id=? AND state='preparing'",
                    (json.dumps(pinned), prompt, json.dumps({'delivery_started':True}), request_id)).rowcount
                if changed != 1:
                    raise SubmissionError('Confirmation changed before delivery.')
        except SubmissionTransportUnavailable:
            self._restore_unsent_authorization(self.row(request_id))
            with self.db() as db:
                db.execute('UPDATE requests SET reason=? WHERE id=? AND state=\'preparing\'',
                           ('Confirmation saved. Waiting for the application agent connection to recover.', request_id))
            return self._public(self.row(request_id))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self._restore_unsent_authorization(self.row(request_id))
            with self.db() as db:
                db.execute("UPDATE requests SET state='blocked',reason=? WHERE id=? AND state='preparing'", (str(exc),request_id))
            return self._public(self.row(request_id))
        try:
            receipt = send_existing(self.root, current['owner'], prompt)
            with self.db() as db:
                db.execute("UPDATE requests SET state='queued',receipt=? WHERE id=? AND state='unknown'", (json.dumps(receipt),request_id))
        except Exception:
            with self.db() as db:
                db.execute("UPDATE requests SET reason='Delivery outcome unknown. Reconcile this exact request; never resend.' WHERE id=?", (request_id,))
        return self._public(self.row(request_id))

    def _public(self, row):
        return {'request_id':row['id'], 'state':row['state'], 'reason':row['reason'], 'owner':row['owner'],
                'job_id':row['job'], 'scope':json.loads(row['preview'])['scope'], 'turn_id':row['turn_id'], 'submitted':False, 'accepted':True,
                'delivery_not_started':row['state']=='blocked' and row['prompt'] is None}

    def _synced_submission(self, row):
        with sqlite3.connect(self.config.jobfeed_db) as db:
            applied = db.execute('SELECT 1 FROM applied WHERE job_id=?', (row['job'],)).fetchone()
        if not applied:
            return None
        closeout = self.root / '.pipeline/submission-closeouts' / f'{row["job"]}.json'
        return self._public(row) | {'state':'submitted', 'submitted':True, 'synced':True,
            'reason':'The formal applied record confirms submission.', 'evidence':str(closeout) if closeout.exists() else 'jobfeed.applied'}

    def _confirmed_submission(self, row):
        item = ApplicationQueue(self.config.jobfeed_db).get(row['job'])
        if not item or not item.application_folder_relpath:
            return None
        folder = self.config.applications_root / item.application_folder_relpath
        if not (folder / 'submission-receipt.json').exists():
            return None
        from jobfeed.submission_evidence import read_confirmation
        from pipeline.jobfeed import Jobfeed
        job = Jobfeed(self.config.jobfeed_db, self.config.jobfeed_root).get_job(row['job'])
        confirmation = read_confirmation(folder, row['job'], job.url, company=job.company)
        from pipeline.orchestrator import ApplicationPipeline
        try:
            outcome = ApplicationPipeline(self.config).finish_submission(row['job'])
            synced = self._synced_submission(row)
            if synced:
                return synced | {'sync_result':outcome}
            reason = 'Website confirmation verified. Formal sync has not completed; retry sync only.'
        except Exception as exc:
            reason = 'Website confirmation verified. Formal sync failed; retry sync only: '+str(exc)
        from pipeline.config import runtime_command
        command = runtime_command(self.root, [str(self.root / '.venv/bin/python'), '-m', 'pipeline.cli',
            'finish-submission', str(row['job'])])
        return self._public(row) | {'state':'sync_pending', 'submitted':True, 'synced':False,
            'reason':reason,
            'evidence':str(confirmation.artifact_path), 'next_action':{'kind':'finish-submission','command':command}}

    def _validate(self, row):
        pinned = json.loads(row['pinned'])
        current = self._preview(pinned['scope'], pinned['job_id'], active=True,
                                attempt_request=row['id'], attempt_turn=row['turn_id'])
        # Entering Submitting in this same exact turn is normal progression.
        current['state'] = pinned['state']
        if current != pinned:
            raise SubmissionError('Confirmed owner, answers, resume, authorization or workflow changed.')
        return current

    def reconcile_pending(self, *, limit=8, budget_seconds=15):
        result = {'checked': 0, 'results': [], 'deferred': False}
        deadline = time.monotonic() + budget_seconds
        path = self.root / '.pipeline/submission-reconcile.lock'
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return result | {'deferred': True}
            pending = sorted(reservations(self.root), key=lambda row:
                             (row.get('last_checked_at') or row['created_at'], row['created_at'], row['id']))
            for saved in pending[:limit]:
                if time.monotonic() >= deadline:
                    result['deferred'] = True
                    break
                try:
                    with admission_lock(self.root, blocking=False):
                        with self.db() as db:
                            db.execute('UPDATE requests SET last_checked_at=? WHERE id=?', (now().isoformat(), saved['id']))
                        result['checked'] += 1
                        result['results'].append(self._status(request_id=saved['id']))
                except BlockingIOError:
                    result['deferred'] = True
                    break
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
                    result['results'].append({'request_id': saved['id'], 'state': 'unavailable',
                                              'reason': 'Reconciliation unavailable: ' + type(exc).__name__})
            if len(pending) > result['checked']:
                result['deferred'] = True
        return result

    def status(self, *, request_id):
        with self.locked():
            return self._status(request_id=request_id)

    def _status(self, *, request_id):
        row = self.row(request_id)
        if not row:
            return {'request_id':request_id, 'state':'not_found', 'reason':'No confirmation recorded.', 'accepted':False, 'submitted':False}
        synced = self._synced_submission(row)
        if synced:
            with self.db() as db:
                db.execute("UPDATE requests SET state='submitted' WHERE id=?", (request_id,))
            return synced
        try:
            confirmed = self._confirmed_submission(row)
            if confirmed:
                with self.db() as db:
                    db.execute('UPDATE requests SET state=? WHERE id=?', (confirmed['state'], request_id))
                return confirmed
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with self.db() as db:
                db.execute("UPDATE requests SET state='blocked',reason=? WHERE id=?", ('Submission receipt requires reconciliation: '+str(exc),request_id))
            return self._public(self.row(request_id))
        if row['state'] == 'preparing':
            self._advance_stopped(request_id)
            row = self.row(request_id)
            if row['state'] == 'preparing':
                return self._public(row)
        if row['state'] not in OPEN:
            if row['state'] == 'blocked' and row['prompt'] and not row.get('terminal_turn'):
                self._terminal_evidence(row)
                row = self.row(request_id)
            return self._public(row)
        return self._reconcile_turn(row)

    def owner_status(self, *, request_id):
        with self.locked():
            row = self.row(request_id)
            if not row:
                return {'state': 'missing', 'submitted': False}
            if row['state'] in {'unknown', 'queued', 'running'} and row['prompt']:
                return self._reconcile_turn(row)
            return self._public(row)

    def _reconcile_turn(self, row):
        try:
            with self.locked(), (nullcontext() if row.get('mode') == 'worker' else worker_stopped(self.root)):
                row = self.row(row['id'])
                if row['state'] == 'preparing':
                    raise SubmissionError('Confirmation interrupted before delivery. No retry is sent.')
                self._validate(row)
                thread = self._thread(json.loads(row['pinned']))
                baseline = json.loads(row['baseline'])
                matches = [turn for turn in thread['turns'] if turn['id'] not in baseline and any(
                    message.get('type') == 'userMessage' and ''.join(p.get('text','') for p in message.get('content',[]) if p.get('type','text') == 'text') == row['prompt']
                    for message in turn.get('items',[]))]
                if len(matches) > 1:
                    raise SubmissionError('Ambiguous owner delivery. Reconcile; do not resend.')
                if matches:
                    turn = matches[0]
                    if row['turn_id'] and row['turn_id'] != turn['id']:
                        raise SubmissionError('Bound owner turn changed.')
                    lifecycle = host_turn_state(thread, turn['id'], json.loads(row['pinned'])['project'])
                    state = 'running' if lifecycle == 'active' else 'turn_ended'
                    with self.db() as db:
                        db.execute('UPDATE requests SET state=?,turn_id=?,terminal_turn=?,reason=NULL WHERE id=?',
                                   (state,turn['id'],turn['id'] if lifecycle in {'completed','interrupted'} else None,row['id']))
        except SubmissionTransportUnavailable:
            with self.db() as db:
                db.execute('UPDATE requests SET reason=? WHERE id=?',
                           ('Delivery reconciliation is waiting for the application agent connection. No message will be resent.', row['id']))
            return self._public(self.row(row['id']))
        except (OSError, ValueError, KeyError, TypeError, StopIteration, sqlite3.Error) as exc:
            if isinstance(exc, RerunError) and str(exc) == 'The application worker is still active or draining.':
                return self._public(row) | {'reason': 'Waiting for existing application work to finish reconciliation.'}
            with self.db() as db:
                db.execute("UPDATE requests SET state='blocked',reason=? WHERE id=?", (str(exc),row['id']))
        return self._public(self.row(row['id']))

    def _terminal_evidence(self, row):
        try:
            pinned = json.loads(row['pinned'])
            thread = self._thread(pinned)
            baseline = json.loads(row['baseline'])
            matches = [turn for turn in thread['turns'] if turn['id'] not in baseline and any(
                message.get('type') == 'userMessage' and ''.join(p.get('text','') for p in message.get('content',[]) if p.get('type','text') == 'text') == row['prompt']
                for message in turn.get('items',[]))]
            if len(matches) == 1 and row['turn_id'] in {None, matches[0]['id']}:
                turn = matches[0]
                if host_turn_state(thread, turn['id'], pinned['project']) in {'completed','interrupted'}:
                    with self.db() as db:
                        db.execute('UPDATE requests SET terminal_turn=? WHERE id=?', (turn['id'], row['id']))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return

    def permits(self, owner, turn, *, scope=None, job=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM requests WHERE owner=? AND turn_id=? AND state='running'", (owner,turn)).fetchone()
        if not row:
            return False
        row = dict(row)
        pinned = json.loads(row['pinned'])
        if (scope is not None and pinned['scope'] != str(scope)) or (job is not None and row['job'] != job):
            return False
        with self.locked(), (nullcontext() if row.get('mode') == 'worker' else worker_stopped(self.root)):
            self._validate(row)
        thread = self._thread(pinned)
        return host_turn_state(thread, turn, pinned['project']) == 'active'


def finish_worker(worker, handle):
    root = worker.pipeline.config.resume_root
    with admission_lock(root):
        handle.close()
        pending = reservations(root, scope=getattr(worker.pipeline, 'manifest_path', ''))
        if not any(row['state'] == 'preparing' for row in pending):
            return []
        service = DashboardSubmissions(worker.pipeline.config)
        return [service._advance_stopped(row['id']) for row in pending if row['state'] == 'preparing']


def consume_worker(worker):
    root = worker.pipeline.config.resume_root
    pending = reservations(root, scope=getattr(worker.pipeline, 'manifest_path', ''))
    if not pending:
        return []
    handle = getattr(worker, '_worker_lock_handle', None)
    if handle is None or handle.closed or Path(handle.name).resolve() != (root / '.pipeline/worker-progress.lock').resolve():
        return []
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    service = DashboardSubmissions(worker.pipeline.config)
    results = []
    with service.locked():
        for saved in pending:
            row = service.row(saved['id'])
            if row['state'] != 'preparing':
                if row.get('mode') == 'worker':
                    results.append(service.status(request_id=row['id']))
                continue
            preview = json.loads(row['preview'])
            monitor = worker.progress_monitor
            if (not monitor or not monitor.ledger or monitor.ledger.scope != preview['scope']
                    or monitor.ledger.controller != preview['controller']):
                continue
            if row['job'] in worker._application_futures:
                continue
            try:
                active = worker._active_application_threads()
            except OSError:
                break  # The Claude inventory is unavailable; the next cycle checks again.
            occupied = {r['job'] for r in reservations(root, scope=preview['scope'])
                        if occupies_slot(r) and r['owner'] not in active}
            occupied.update(job for job in worker._application_futures
                            if (worker.pipeline.queue.get(job).codex_application_session_id not in active))
            limit = min(worker.settings.application_workers, worker.pipeline.batch_limit)
            if row['owner'] not in active and len(active) + len(occupied) >= limit:
                with service.db() as db:
                    db.execute("UPDATE requests SET reason=? WHERE id=?", ('Confirmation saved. Waiting for an application slot.', row['id']))
                continue
            results.append(service._advance(row['id'], mode='worker'))
    return results


def permits(config, owner, turn, *, scope=None, job=None):
    if not (config.resume_root / '.pipeline/dashboard-submissions.sqlite3').is_file():
        return False
    try:
        return DashboardSubmissions(config).permits(owner,turn,scope=scope,job=job)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, AppServerError, SubmissionTransportUnavailable, sqlite3.Error):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--jobfeed-db', type=Path)
    parser.add_argument('command', choices=['preview','confirm','status','owner-status','reconcile-pending'])
    args = parser.parse_args()
    config = PipelineConfig.discover(args.root)
    if args.jobfeed_db:
        from dataclasses import replace
        config = replace(config, jobfeed_db=args.jobfeed_db)
    try:
        body = json.load(sys.stdin)
        if args.command == 'reconcile-pending':
            if body != {}:
                raise SubmissionError('Reconciliation accepts only an empty object.')
            if not (config.resume_root / '.pipeline/dashboard-submissions.sqlite3').is_file():
                print(json.dumps({'checked': 0, 'results': [], 'deferred': False}))
                return 0
        service = DashboardSubmissions(config)
        if args.command == 'reconcile-pending':
            result = service.reconcile_pending()
        elif args.command == 'owner-status':
            result = service.owner_status(**body)
        else:
            result = getattr(service, args.command)(**body)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc),'code':'conflict'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
