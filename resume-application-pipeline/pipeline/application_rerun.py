"""One explicit dashboard click continues one original applier without resuming its batch."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

from pipeline.cancellation import CancellationStore, utc_now
from pipeline.jobfeed import Jobfeed
from pipeline.store import ApplicationQueue

OPEN_STATES = ('unknown', 'queued', 'running')
TERMINAL_HOST_STATES = {'completed', 'interrupted'}
TURN_ENDED_REASON = 'The applier turn ended without completing.'
PREPARATION_STATES = {'Applying', 'Needs Input', 'Ready for Review', 'Reviewing',
                      'Awaiting Approval', 'Submit Authorized'}


class RerunError(ValueError):
    def __init__(self, message, code='conflict'):
        super().__init__(message)
        self.code = code


def read_thread(owner):
    from pipeline.codex_app_server import CodexAppServer
    with CodexAppServer(approve_requests=False) as server:
        thread = server.request('thread/read', {'threadId': owner, 'includeTurns': True})['thread']
    if thread.get('id') != owner or not isinstance(thread.get('turns'), list):
        raise RerunError('The original applier could not be verified.', 'unavailable')
    return thread


def host_turn_state(thread, turn_id, project):
    from pipeline.claude_codex_bridge import read_turn_lifecycle
    proof = read_turn_lifecycle(thread.get('path'), thread['id'], turn_id, project)
    state = proof.get('state')
    if state not in TERMINAL_HOST_STATES | {'active'}:
        raise RerunError('The original applier lifecycle could not be verified.', 'unavailable')
    return state


def send_existing(root, owner, prompt):
    from pipeline.claude_codex_bridge import queue_message
    return {'thread_id': owner, 'queued_message_id': queue_message(owner, prompt, root), 'status': 'queued'}


def _object(path):
    if path.stat().st_size > 2_000_000:
        raise RerunError('Application metadata is too large.', 'unavailable')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RerunError('Application metadata is invalid.', 'unavailable')
    return value


def _confined(root, path):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise RerunError('Application metadata is outside its runtime.')
    return resolved


def _readonly(path):
    return sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)


def _public(row):
    return {name: row[name] for name in ('request_id', 'batch_id', 'job_id', 'owner_thread_id',
                                        'state', 'reason', 'created_at', 'turn_id')}


def _matches(turn, row):
    return turn.get('id') not in json.loads(row['baseline']) and any(
        item.get('type') == 'userMessage' and
        ''.join(part.get('text', '') for part in item.get('content', []) if part.get('type', 'text') == 'text') == row['prompt']
        for item in turn.get('items', []))


class ApplicationReruns:
    def __init__(self, root, jobfeed_db):
        self.root, self.jobfeed_db = Path(root).resolve(), Path(jobfeed_db).resolve()
        gate_path = _confined(self.root, self.root / '.pipeline/execution-gates.sqlite3')
        self.gate = CancellationStore(gate_path)

    def context(self, batch_id, job_id, *, owner=None, idle=True):
        if not isinstance(batch_id, str) or Path(batch_id).name != batch_id or batch_id in {'.', '..'}:
            raise RerunError('Invalid application batch.', 'invalid')
        if type(job_id) is not int or job_id < 1:
            raise RerunError('Invalid application job.', 'invalid')
        monitor = _object(_confined(self.root / 'monitor-output', self.root / 'monitor-output' / batch_id / 'monitor.json'))
        raw_scope = Path(monitor['source'])
        scope = _confined(self.root / '.pipeline/batches', raw_scope if raw_scope.is_absolute() else self.root / raw_scope)
        manifest = _object(scope)
        candidates = manifest.get('ordered_candidates', [])
        ids = [candidate.get('id') for candidate in candidates]
        if not 1 <= len(ids) <= 200 or any(type(i) is not int or i < 1 for i in ids) or len(set(ids)) != len(ids) or job_id not in ids:
            raise RerunError('The job is not in the current batch.')
        controller = manifest.get('controller_thread_id')
        with closing(_readonly(_confined(self.root, self.root / '.pipeline/blockers.sqlite3'))) as db:
            scope_row = db.execute('SELECT controller,active FROM dq_scopes WHERE scope=?', (str(scope),)).fetchone()
            member = db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (str(scope), str(job_id))).fetchone()
            if not controller or not scope_row or scope_row[0] != controller or not member or member[0] != 'active':
                raise RerunError('The controller or job membership changed.')
            if db.execute("SELECT 1 FROM dq_actions a JOIN dq_issues i ON i.id=a.issue WHERE a.scope=? AND i.job=? AND a.state IN ('pending','prepared','unknown','sent') LIMIT 1", (str(scope), str(job_id))).fetchone():
                raise RerunError('An earlier controller action must be reconciled first.')
        if scope_row[1]:
            raise RerunError('Stop this batch before rerunning a single application.')
        handoff = _object(_confined(self.root, scope.with_name(scope.stem + '-handoff.json')))
        if handoff.get('manifest') != str(scope) or handoff.get('controller') != controller or handoff.get('status') != 'stopped_by_user':
            raise RerunError('The batch worker is not confirmed stopped.')
        queue = ApplicationQueue(self.jobfeed_db)
        item = queue.get(job_id)
        if not item or not item.codex_application_session_id:
            raise RerunError('The original applier is unavailable.')
        original = item.codex_application_session_id
        if owner is not None and owner != original:
            raise RerunError('The original applier changed. Refresh before rerunning.')
        if str(item.state) not in PREPARATION_STATES:
            raise RerunError('This application state cannot be rerun. Verify its outcome first.')
        if queue.paused(job_id):
            raise RerunError('This application is paused for a critical review.')
        jobfeed = Jobfeed(self.jobfeed_db, self.root.parent / 'jobfeed')
        job = jobfeed.get_job(job_id, require_open=True)
        if jobfeed.is_applied(job_id):
            raise RerunError('This application is already submitted.')
        if not item.application_folder_relpath:
            raise RerunError('The original application materials are missing.')
        folder = _confined(self.root / 'applications', self.root / 'applications' / item.application_folder_relpath)
        for name in ('submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json'):
            if (folder / name).exists():
                raise RerunError('Submission evidence exists. Verify it before any further application work.')
        if (self.root / '.pipeline/submission-closeouts' / f'{job_id}.json').exists():
            raise RerunError('This application has submission closeout work.')
        with self.gate._db() as db:
            if db.execute("SELECT 1 FROM application_reruns r JOIN application_restarts s USING(request_id) WHERE r.job_id=? AND r.state='blocked' AND s.phase IN ('create_unknown','candidate_received','adopting')", (job_id,)).fetchone():
                raise RerunError('A previous replacement creation or adoption must be reconciled before another restart.')
            if db.execute("SELECT 1 FROM application_reruns WHERE job_id=? AND state='blocked' AND turn_id IS NULL AND json_extract(receipt, '$.delivery_started')=1", (job_id,)).fetchone():
                raise RerunError('A cancelled rerun has an uncertain delivery. Verify its original applier first.')
            binding = db.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (original,)).fetchone()
            if not binding or tuple(binding) != (str(scope), job_id):
                raise RerunError('The original applier binding changed.')
            stops = stop_snapshot(db, str(scope), job_id)
        if not scope_row[1] and not stops['scope']:
            raise RerunError('This batch is inactive without an explicit recoverable user stop.')
        deliveries = self.root / '.pipeline/desktop-deliveries.sqlite3'
        if deliveries.exists():
            with closing(_readonly(_confined(self.root, deliveries))) as db:
                if db.execute("SELECT 1 FROM deliveries WHERE job=? AND state IN ('pending','unknown','sent') LIMIT 1", (job_id,)).fetchone():
                    raise RerunError('An earlier applier delivery must be reconciled first.')
        thread = read_thread(original)
        project = Path(manifest.get('desktop_project_root', self.root)).resolve()
        if Path(thread.get('cwd', '')).resolve() != project or thread.get('archived') or thread.get('originator') != 'Codex Desktop':
            raise RerunError('The original applier project or availability changed.')
        turns = thread['turns']
        if not turns or any(not isinstance(turn.get('id'), str) for turn in turns):
            raise RerunError('The original applier has no verifiable turn history.', 'unavailable')
        from pipeline.results import parse_application_result
        reported_results = []
        for turn in turns:
            for item_message in turn.get('items', []):
                if item_message.get('type') != 'agentMessage':
                    continue
                try:
                    result = parse_application_result(item_message.get('text', ''))
                except (ValueError, TypeError):
                    continue
                reported_results.append((turn, result))
                if result.get('status') in {'submitted', 'submission_unknown', 'already_applied', 'unavailable'} or result.get('success_evidence'):
                    raise RerunError('The applier reported a terminal or uncertain outcome. Verify it first.')
        if reported_results and reported_results[-1][1].get('status') == 'needs_verification':
            from pipeline.application_restart import verified_preparation
            if not verified_preparation(thread, folder, job, project, reported_results[-1][0]['id']):
                raise RerunError('The applier reported a terminal or uncertain outcome. Verify it first.')
        if idle:
            status = thread.get('status', {})
            kind = status.get('type') if isinstance(status, dict) else status
            if kind not in {'idle', 'notLoaded'} or host_turn_state(thread, turns[-1]['id'], project) not in TERMINAL_HOST_STATES:
                raise RerunError('The original applier is busy or waiting for approval.')
            limit = manifest.get('max_parallel')
            if type(limit) is not int or not 1 <= limit <= 200:
                raise RerunError('The batch concurrency limit is unavailable.')
            with closing(_readonly(self.jobfeed_db)) as db:
                peers = [row[0] for row in db.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id IN (' + ','.join('?' for _ in ids) + ') AND codex_application_session_id IS NOT NULL', ids)]
            active = set()
            for peer in set(peers) - {original}:
                peer_thread = read_thread(peer)
                if not peer_thread.get('turns') or host_turn_state(peer_thread, peer_thread['turns'][-1]['id'], project) == 'active':
                    active.add(peer)
            if len(active) >= limit:
                raise RerunError('The controller has no free application capacity.')
        return dict(scope=str(scope), controller=controller, owner=original, job=job,
                    limit=manifest.get('max_parallel'), active_peers=active if idle else None,
                    folder=folder, stops=stops, thread=thread, project=project)

    def latest(self, batch_id, job_id):
        with self.gate._db() as db:
            row = db.execute('SELECT * FROM application_reruns WHERE batch_id=? AND job_id=? ORDER BY created_at DESC LIMIT 1', (batch_id, job_id)).fetchone()
        return dict(row) if row else None

    def reconcile(self, row, *, strict=False):
        if row['source'] == 'local_dashboard_restart':
            from pipeline.application_restart import Restarts
            return Restarts(self).reconcile(row, strict=strict)
        repair_terminal_observation = row['state'] == 'blocked' and row['reason'] == TURN_ENDED_REASON
        if row['state'] not in OPEN_STATES and not repair_terminal_observation:
            return row
        try:
            context = self.context(row['batch_id'], row['job_id'], owner=row['owner_thread_id'], idle=False)
            matches = [turn for turn in context['thread']['turns'] if _matches(turn, row)]
            if context['stops'] != json.loads(row['stops']):
                raise RerunError('A newer stop revoked this rerun.')
            if len(matches) > 1:
                raise RerunError('The rerun turn is ambiguous. Verify the applier before continuing.')
            if strict and not matches:
                raise RerunError('The current rerun turn could not be verified.', 'unavailable')
            if matches:
                turn = matches[0]
                if row['turn_id'] is not None and row['turn_id'] != turn['id']:
                    raise RerunError('The bound rerun turn changed. Verify the original applier.')
                lifecycle = host_turn_state(context['thread'], turn['id'], context['project'])
                state = {'completed': 'completed', 'interrupted': 'blocked', 'active': 'running'}[lifecycle]
                reason = TURN_ENDED_REASON if state == 'blocked' else None
                with self.gate._db() as db:
                    db.execute("UPDATE application_reruns SET state=?,turn_id=?,reason=? WHERE request_id=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason=?))", (state, turn['id'], reason, row['request_id'], TURN_ENDED_REASON))
        except RerunError as exc:
            if exc.code == 'unavailable':
                if strict:
                    raise
                return row
            with self.gate._db() as db:
                db.execute("UPDATE application_reruns SET state='blocked',reason=? WHERE request_id=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason=?))", (str(exc), row['request_id'], TURN_ENDED_REASON))
        except (OSError, ValueError, KeyError, sqlite3.Error):
            if strict:
                raise
            return row
        with self.gate._db() as db:
            return dict(db.execute('SELECT * FROM application_reruns WHERE request_id=?', (row['request_id'],)).fetchone())

    def status(self, batch_id, job_id):
        row = self.latest(batch_id, job_id)
        if row:
            row = self.reconcile(row)
        if row and row['source'] == 'local_dashboard_restart':
            from pipeline.application_restart import Restarts
            return Restarts(self).status(row)
        if row and row['state'] in OPEN_STATES:
            return dict(eligible=False, reason='A rerun is already in progress; its delivery will not be repeated.', owner_thread_id=row['owner_thread_id'], request=_public(row))
        try:
            with worker_stopped(self.root):
                context = self.context(batch_id, job_id)
            return dict(eligible=True, reason=None, owner_thread_id=context['owner'], request=_public(row) if row else None)
        except RerunError as exc:
            return dict(eligible=False, reason=str(exc), owner_thread_id=row['owner_thread_id'] if row else None, request=_public(row) if row else None)
        except Exception:
            return dict(eligible=False, reason='Current application or applier state could not be verified.', owner_thread_id=None, request=_public(row) if row else None)

    def request(self, batch_id, job_id, request_id, expected_owner):
        from pipeline.submission_reservation import admission_lock, assert_available
        try:
            with admission_lock(self.root, blocking=False), worker_stopped(self.root):
                assert_available(self.root, job_id)
                return self._request(batch_id, job_id, request_id, expected_owner)
        except BlockingIOError:
            raise RerunError('Another application admission is in progress.') from None

    def _request(self, batch_id, job_id, request_id, expected_owner):
        if str(UUID(request_id)) != request_id:
            raise RerunError('A random UUID request ID is required.', 'invalid')
        with self.gate._db() as db:
            old = db.execute('SELECT * FROM application_reruns WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if (old['batch_id'], old['job_id'], old['owner_thread_id']) != (batch_id, job_id, expected_owner):
                raise RerunError('This request ID belongs to a different application.')
            row = self.reconcile(dict(old))
            return dict(eligible=False, reason=row['reason'], owner_thread_id=row['owner_thread_id'], request=_public(row))
        context = self.context(batch_id, job_id, owner=expected_owner)
        prompt = (f'[application-rerun:{request_id}]\n'
                  f'The user clicked Rerun application in the local dashboard for job {job_id} only. '
                  f'Continue as its original applier. Read the existing application materials in {context["folder"]}. '
                  'This authorizes one preparation/refill turn while the batch and worker remain stopped. '
                  'First inspect the original saved form and resources. Reuse the original form if it exists; '
                  'reopen the same application URL only if the original form is missing. Do not describe a new page as restored unsaved state. '
                  'Reuse the exact existing resume selection and confirmed personal facts. Fill only individually approved answer-bank versions; '
                  'leave unapproved or missing answers pending review. STOP BEFORE SUBMIT, regardless of any older submission grant. '
                  'Do not create another agent, restart the worker, resume the batch, or work on another job. '
                  'Save progress, update resource-contract.json and saved tab associations, mark the form for handoff, '
                  'and report the preparation outcome. This click does not approve new answers or authorize final submission.')
        with self.gate._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current_stops = stop_snapshot(db, context['scope'], job_id)
            if current_stops != context['stops']:
                raise RerunError('The application stop changed. Refresh before rerunning.')
            outstanding = db.execute("SELECT * FROM application_reruns WHERE job_id=? AND state IN ('unknown','queued','running')", (job_id,)).fetchone()
            if outstanding:
                return dict(eligible=False, reason='A rerun is already in progress.', owner_thread_id=outstanding['owner_thread_id'], request=_public(outstanding))
            reservations = {row[0] for row in db.execute("SELECT owner_thread_id FROM application_reruns WHERE scope=? AND state IN ('unknown','queued','running')", (context['scope'],))}
            if len(reservations | context['active_peers']) >= context['limit']:
                raise RerunError('The controller has no free application capacity.')
            db.execute('INSERT INTO application_reruns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id,batch_id,job_id,context['scope'],context['controller'],expected_owner,str(self.jobfeed_db),
                        'unknown',None,utc_now(),None,prompt,json.dumps([turn['id'] for turn in context['thread']['turns']]),json.dumps(current_stops),None,'local_dashboard_click'))
        try:
            fresh = self.context(batch_id, job_id, owner=expected_owner)
            with self.gate._db() as db:
                admitted = db.execute('SELECT state,stops FROM application_reruns WHERE request_id=?', (request_id,)).fetchone()
                if admitted['state'] != 'unknown' or fresh['stops'] != json.loads(admitted['stops']):
                    raise RerunError('A newer stop revoked this rerun.')
        except Exception as exc:
            with self.gate._db() as db:
                db.execute("UPDATE application_reruns SET state='blocked',reason=? WHERE request_id=? AND state='unknown'", (str(exc) if isinstance(exc, RerunError) else 'Application state changed or could not be verified before delivery.', request_id))
            row = self.latest(batch_id, job_id)
            return dict(eligible=False, reason=row['reason'], owner_thread_id=expected_owner, request=_public(row))
        try:
            with self.gate._db() as db:
                db.execute("UPDATE application_reruns SET receipt=? WHERE request_id=? AND state='unknown'", (json.dumps({'delivery_started': True}), request_id))
                row = db.execute('SELECT state FROM application_reruns WHERE request_id=?', (request_id,)).fetchone()
                if row['state'] != 'unknown':
                    raise RerunError('A newer stop revoked this rerun.')
            receipt = send_existing(self.root, expected_owner, prompt)
            receipt['delivery_started'] = True
            with self.gate._db() as db:
                db.execute("UPDATE application_reruns SET state='queued',receipt=? WHERE request_id=? AND state='unknown'", (json.dumps(receipt), request_id))
        except Exception:
            with self.gate._db() as db:
                db.execute("UPDATE application_reruns SET reason=? WHERE request_id=? AND state='unknown'", ('Delivery is uncertain. Waiting for the original applier; this request will not be resent.', request_id))
        row = self.latest(batch_id, job_id)
        row = self.reconcile(row)
        return dict(eligible=False, reason=row['reason'], owner_thread_id=expected_owner, request=_public(row))


@contextmanager
def worker_stopped(root):
    path = root / '.pipeline/worker-progress.lock'
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RerunError('The application worker is still active or draining.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def stop_snapshot(db, scope, job_id):
    if db.execute("SELECT 1 FROM execution_user_updates WHERE scope=? AND job_id=? AND kind='submission'", (scope, job_id)).fetchone():
        raise RerunError('The user already reported this application submitted.')
    result = {}
    for key, query, params in (
        ('scope', 'SELECT * FROM execution_scope_cancellations WHERE scope=?', (scope,)),
        ('job', 'SELECT * FROM execution_cancellations WHERE scope=? AND job_id=?', (scope, job_id)),
    ):
        row = db.execute(query, params).fetchone()
        if row and row['reason'] != 'stopped_by_user':
            raise RerunError('This application has a non-recoverable cancellation.')
        result[key] = dict(row) if row else None
    return result


def permits_turn(gate, owner, turn_id):
    if not turn_id:
        return False
    with gate._db() as db:
        row = db.execute("SELECT * FROM application_reruns WHERE owner_thread_id=? AND source!='local_dashboard_restart' AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason=?)) ORDER BY created_at DESC LIMIT 1", (owner, TURN_ENDED_REASON)).fetchone()
    if not row:
        return False
    service = ApplicationReruns(gate.path.parent.parent, row['jobfeed_db'])
    try:
        current = service.reconcile(dict(row), strict=True)
        return current['state'] == 'running' and current['turn_id'] == turn_id
    except Exception:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--jobfeed-db', type=Path, required=True)
    parser.add_argument('command', choices=['status', 'dashboard-request'])
    args = parser.parse_args(argv)
    try:
        body = json.load(sys.stdin)
        expected = {'batch_id', 'job_id'} | ({'request_id', 'expected_owner'} if args.command == 'dashboard-request' else set())
        if set(body) - ({'dashboard_url'} if args.command == 'dashboard-request' else set()) != expected:
            raise RerunError('Invalid rerun command fields.', 'invalid')
        service = ApplicationReruns(args.root, args.jobfeed_db)
        if args.command == 'status':
            result = service.status(**body)
        else:
            from pipeline.application_restart import Restarts
            result = Restarts(service).request(**body)
        print(json.dumps(result))
        return 0
    except RerunError as exc:
        print(json.dumps({'code': exc.code, 'error': str(exc)}))
    except Exception:
        print(json.dumps({'code': 'unavailable', 'error': 'Current application state could not be verified.'}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
