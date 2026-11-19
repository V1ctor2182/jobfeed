"""Read-only progress checks for one batch.

Only the output directory is writable; business actions remain in the sole worker.
"""
from __future__ import annotations
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from pipeline.blocker_ledger import BlockerLedger, handoff_message, wake_message
from pipeline.progress_report import render_report, render_blockers
from pipeline.submission_state import valid_user_submission

STALL_MINUTES = 10


def stamp():
    return datetime.now(timezone.utc).isoformat()


def existing_resume_readiness(config, frozen_catalog=None):
    from pipeline.resume_catalog import ResumeCatalog
    try:
        if frozen_catalog is None:
            entries = ResumeCatalog.load(config.resume_root / 'resume-catalog.json').entries
        else:
            from pipeline.resume_catalog import verify_catalog
            entries = verify_catalog(config.resume_root, frozen_catalog)['entries']
        materials = {'state': 'available', 'resumes': [{'id': e['id'], 'label': e['label']} for e in entries]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        materials = {'state': 'missing_or_invalid', 'error': str(exc)}
    return {'mode': 'read_only', 'resume_mode': 'selected_existing', 'resume_catalog': materials,
            'runtime_connectivity': 'not_probed'}


def attach_resources(snapshot, resources):
    snapshot['coordination'] = resources
    for job in snapshot.get('jobs', []):
        identity = job['job_id']
        job['tabs'] = [r for r in resources.get('tabs', []) if r.get('job_id', r.get('job')) == identity]
        job['resource_claims'] = [r for r in resources.get('claims', []) if r.get('job_id', r.get('job')) == identity and r.get('state', r.get('status')) != 'released']
        job['resource_waits'] = [r for r in resources.get('waits', []) if r.get('job_id', r.get('job')) == identity and r.get('state', r.get('status', 'waiting')) == 'waiting']
    return snapshot


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'monitor.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_json(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value


def structured_close_proof(receipt):
    evidence = receipt.get('tab_close_evidence')
    if not isinstance(evidence, dict):
        return False
    tab_id = str(receipt.get('tab_id', ''))
    before = evidence.get('before', [])
    before = [before] if isinstance(before, dict) else before
    if not tab_id or not isinstance(before, list) or not any(isinstance(t, dict) and str(t.get('id')) == tab_id for t in before):
        return False
    method = str(evidence.get('method', ''))
    absent = evidence.get('after_id_absent') is True or evidence.get('after_target_present') is False
    ids = evidence.get('closed_tab_ids')
    return absent and 'openTabs' in method and (ids is None or (isinstance(ids, list) and tab_id in map(str, ids)))


def timestamped_close_proof(receipt, cursor):
    """Legacy receipt: timestamp + named tabs + verified original-owner final."""
    if not cursor.get('owner_verified') or not cursor.get('result_close_confirmed'):
        return False
    ids = receipt.get('closed_tab_ids')
    if not isinstance(ids, list) or not ids or not all(str(i).isdigit() for i in ids):
        return False
    try:
        times = [datetime.fromisoformat(x.replace('Z', '+00:00')) for x in
                 (receipt['closed_verified_at'], cursor['result_at'], cursor['last_tool_at'])]
        return all(t.tzinfo is not None for t in times) and times[0] <= times[1] and times[0] <= times[2]
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def global_close_proof(receipt):
    """Accept named original tabs and explicit global inventory evidence only."""
    proof = receipt.get('tab_cleanup_evidence')
    if isinstance(proof, dict):
        ids = proof.get('target_tab_ids')
        if (isinstance(ids, list) and ids and all(str(i).isdigit() for i in ids)
                and 'user.openTabs' in str(proof.get('method', ''))
                and 'global' in str(proof.get('method', '')).lower()
                and proof.get('target_ids_present_after') == []
                and proof.get('checked_at')):
            return True
    proof = receipt.get('tab_close_evidence')
    if isinstance(proof, dict):
        # Earlier tab ownership plus a later global inventory proves absence;
        # a saved pre-close global inventory is not required.
        original = proof.get('original_tab_ids')
        ownership = proof.get('ownership_evidence', {})
        disappearance = proof.get('global_disappearance_evidence', {})
        if isinstance(ownership, dict) and isinstance(disappearance, dict):
            after = disappearance.get('after_global_tabs')
            tab = str(receipt.get('tab_id', ''))
            if (tab.isdigit() and original == [tab]
                    and str(ownership.get('tab_id')) == tab and ownership.get('url')
                    and 'global' in str(proof.get('method', '')).lower()
                    and 'user.openTabs' in str(proof.get('method', ''))
                    and proof.get('remaining_tab_ids') == []
                    and disappearance.get('job_id') == receipt.get('job_id')
                    and disappearance.get('original_tab_ids') == original
                    and disappearance.get('checked_at')
                    and isinstance(after, list)
                    and all(isinstance(t, dict) and t.get('id') is not None for t in after)
                    and tab not in {str(t['id']) for t in after}):
                return True
        tab = str(receipt.get('tab_id', ''))
        if (tab and str(proof.get('tab_id')) == tab
                and 'user.openTabs' in str(proof.get('verification_method', ''))
                and proof.get('absent_after_close') is True
                and proof.get('before_url') and receipt.get('tabs_closed_at')):
            return True
        # The original tabs were already gone: named ids plus empty global listings.
        absent, checks = proof.get('confirmed_absent_tab_ids'), proof.get('global_checks')
        if (receipt.get('tabs_closed') is True and 'user.openTabs' in str(proof.get('method', ''))
                and isinstance(absent, list) and absent and all(str(i).isdigit() for i in absent)
                and isinstance(checks, list) and checks
                and all(isinstance(c, dict) and c.get('checked_at') and c.get('matching_tabs') == [] for c in checks)):
            return True
    proof = receipt.get('tabs_closed_evidence')
    tab = str(receipt.get('tab_id', ''))
    if not tab:
        return False
    if isinstance(proof, dict):
        return (str(proof.get('tab_id')) == tab and 'openTabs' in str(proof.get('method', ''))
                and proof.get('original_tab_absent') is True and bool(proof.get('checked_at')))
    if isinstance(proof, str):
        return (tab in proof and 'openTabs' in proof and 'global' in proof.lower()
                and any(word in proof.lower() for word in ('neither', 'absent', 'no longer'))
                and tab in map(str, receipt.get('closed_tab_ids', [])))
    return False


class LocalSources:
    """Explicit input paths; every SQL connection is read-only, no initialize()."""
    def __init__(self, root, manifest, jobfeed_db):
        self.root = Path(root).resolve()
        self.manifest = Path(manifest).resolve()
        self.jobfeed_db = Path(jobfeed_db).resolve()

    def collect(self, cursors, *, authorized_manifest=None):
        manifest = read_json(self.manifest) if authorized_manifest is None else authorized_manifest
        from pipeline.claude_dispatch import is_claude_owner
        from pipeline.claude_progress import claude_cursor
        from pipeline.store import Hold
        followup = read_json(self.root / manifest['monitor_followup_file']).get('candidates', []) if manifest.get('monitor_followup_file') else []
        candidates = {int(j['id']): j for j in followup + manifest.get('carryover_candidates', []) + manifest['ordered_candidates']}
        counted = {int(j['id']) for j in manifest['ordered_candidates']}
        jobs, errors, rows, advice = [], [], {}, {}
        applied = set()
        postings = {}
        try:
            with closing(sqlite3.connect(self.jobfeed_db.as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                ids = list(candidates)
                marks = ','.join('?' for _ in ids)
                rows = {r['job_id']: dict(r) for r in db.execute(f'SELECT * FROM application_queue WHERE job_id IN ({marks})', ids)}
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='application_terminal'").fetchone():
                    for terminal_row in db.execute(f'SELECT * FROM application_terminal WHERE job_id IN ({marks})', ids):
                        rows.setdefault(terminal_row['job_id'], dict(terminal_row))
                from pipeline.jobfeed import Jobfeed
                predicate = Jobfeed._applied_predicate(db)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='application_advice'").fetchone():
                    advice = {r['job_id']: dict(r) for r in db.execute(f"SELECT * FROM application_advice WHERE status != 'resolved' AND job_id IN ({marks}) ORDER BY id", ids)}
                applied = {r[0] for r in db.execute(f'SELECT j.id FROM jobs j WHERE j.id IN ({marks}) AND ({predicate})', ids)}
                postings = {row['id']: (row['url'], row['company']) for row in db.execute(f'SELECT id,url,company FROM jobs WHERE id IN ({marks})', ids)}
        except (OSError, sqlite3.Error) as exc:
            errors.append('jobfeed_readback:' + type(exc).__name__)
        for job_id, candidate in candidates.items():
            row = rows.get(job_id, {})
            problems = []
            close = self._artifact(self.root / '.pipeline/submission-closeouts' / f'{job_id}.json', job_id, problems)
            terminal = self._artifact(self.root / '.pipeline/closed-jobs' / f'{job_id}.json', job_id, problems)
            folder = row.get('application_folder_relpath') or close.get('folder') or terminal.get('folder')
            receipt = {}
            user_confirmation = {}
            terminal_proof = {}
            if folder:
                folder_path = (self.root / 'applications' / folder).resolve()
                if not folder_path.is_relative_to(self.root / 'applications'):
                    problems.append('folder_outside_applications')
                else:
                    receipt = self._artifact(folder_path / 'submission-receipt.json', job_id, problems)
                    terminal_proof = self._artifact(folder_path / 'tab-close-evidence.json', job_id, problems)
                    user_confirmation = self._artifact(folder_path / 'user-submission-confirmation.json', job_id, problems)
            owner = row.get('codex_application_session_id') or close.get('thread_id') or terminal.get('thread_id')
            if row.get('codex_application_session_id') and close.get('thread_id') and owner != close['thread_id']:
                problems.append('owner_conflict')
            cursor = {}
            claude = is_claude_owner(owner)
            if claude:
                try:
                    cursor, launch_problems = claude_cursor(self.root, job_id, owner)
                    problems.extend(launch_problems)
                except (OSError, sqlite3.Error):
                    problems.append('session_unreadable')
            elif owner:
                problems.append('session_unreadable')
            submitted = False
            if folder and 'folder_outside_applications' not in problems:
                from jobfeed.submission_evidence import read_confirmation, EvidenceValidationError
                try:
                    url, company = postings.get(job_id, ('', None))
                    read_confirmation(folder_path, job_id, url, company=company)
                    submitted = True
                except EvidenceValidationError:
                    if receipt:
                        problems.append('missing_submission_evidence')
            synced = job_id in applied
            close_result = receipt.get('close_result') or {}
            close_proof = (isinstance(close_result, dict) and close_result.get('status') in {'already_applied', 'submitted'}
                           and '标签关闭：完成' in str(close_result.get('reason', '')))
            close_proof = close_proof or bool(receipt.get('closed_tab_ids') and receipt.get('tab_close_evidence'))
            # Direct owner closeouts use this schema; require evidence on both records.
            close_proof = close_proof or bool(isinstance(receipt.get('close_evidence'), str)
                and receipt['close_evidence'].strip() and isinstance(close.get('close_evidence'), str)
                and close['close_evidence'].strip() and close.get('tabs_closed') is True)
            close_proof = close_proof or timestamped_close_proof(receipt, cursor) or structured_close_proof(receipt) or global_close_proof(receipt)
            closed = bool(submitted and synced and close_proof and receipt.get('tabs_closed') is True and close.get('complete') is True)
            if submitted and synced and receipt.get('tabs_closed') is True and not close_proof:
                problems.append('close_evidence_missing')
            if 'owner_conflict' in problems:
                submitted = closed = False
            if synced and not submitted:
                problems.append('missing_submission_evidence')
            paused = job_id in manifest.get('suspended_job_ids', []) or row.get('state') == 'Cancelled'
            state = cursor.get('turn_state', 'unknown' if owner else 'not_started')
            blocked = cursor.get('result_status') if state in {'ended', 'interrupted'} else None
            current_advice = advice.get(job_id, {})
            if current_advice and current_advice.get('luna_session_id') != owner:
                problems.append('advice_owner_conflict')
            elif current_advice.get('status') in {'needs_user', 'critical', 'controller_required', 'pending', 'running', 'answered', 'delivering'}:
                blocked = 'needs_advice'
            if row.get('state') == 'Submission Unknown':
                blocked = 'submission_unknown'
            if (blocked in {'submitted', 'already_applied'} and not submitted
                    and not (terminal.get('complete') and terminal.get('jobfeed_ignored')
                             and terminal.get('owner_reassigned_to'))):
                problems.append('submission_claim_requires_receipt')
            if blocked not in {'needs_input', 'needs_verification', 'needs_advice', 'submission_unknown', 'retryable_error', 'needs_strong_model'}:
                blocked = None
            user_submitted = valid_user_submission(user_confirmation, job_id)
            submission_complete = bool(submitted)
            closed = bool(submission_complete and synced and close_proof and receipt.get('tabs_closed') is True and close.get('complete') is True and 'owner_conflict' not in problems)
            if submitted:
                blocked = None
            elif user_submitted:
                blocked = 'submission_unknown'
                if 'missing_submission_evidence' not in problems:
                    problems.append('missing_submission_evidence')
            jobs.append(dict(
                submission_complete=submission_complete, user_submitted=user_submitted, job_id=job_id, company=candidate.get('company', ''), title=candidate.get('title', ''),
                company_key=candidate.get('company_key') or candidate.get('company', '').strip().casefold(),
                advice_id=current_advice.get('id'), advice_revision=current_advice.get('revision'),
                counted=job_id in counted, owner=owner, submitted=submitted, synced=synced, closed=closed,
                terminal_closed=bool(terminal.get('tabs_closed') and terminal.get('tab_close_evidence')) or bool(terminal_proof.get('closed_tab_ids') and terminal_proof.get('remaining_tab_ids') == [] and terminal_proof.get('method') == 'global user.openTabs' and terminal_proof.get('checked_at')),
                terminal=bool(terminal.get('complete') and terminal.get('evidence')) or row.get('state') in {'Cancelled','Skipped','Unavailable'},
                paused=paused, held=bool((hold := Hold.parse(row.get('hold'))) and hold.active),
                queue_state='Unavailable' if terminal.get('complete') else row.get('state'), turn_state=state, turn_id=cursor.get('turn_id'), turn_at=cursor.get('turn_at'),
                last_tool_at=cursor.get('last_tool_at'), blocked=blocked, blocker_kind=cursor.get('blocker_kind'), result_hash=cursor.get('result_hash'),
                result_status=cursor.get('result_status'), result_at=cursor.get('result_at'),
                result_reason=cursor.get('result_reason'), result_quote=cursor.get('result_quote'),
                errors=problems, evidence_path=str(folder_path / 'submission-receipt.json') if folder else None))
            requests = cursor.get('requests', {})
            fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')
            usage = {key: sum(record.get(key, 0) for record in requests.values()) for key in fields}
            usage.update(requests=len(requests), total_tokens=usage['input_tokens'] + usage['output_tokens'])
            jobs[-1].update(usage=usage, first_started_at=cursor.get('first_started_at'),
                            chat_url=None)
        return manifest, jobs, errors

    @staticmethod
    def _artifact(path, job_id, problems):
        if not path.exists():
            return {}
        try:
            value = read_json(path)
            if value.get('job_id') != job_id:
                raise ValueError('identity mismatch')
            return value
        except (OSError, ValueError):
            problems.append(path.name + ':invalid')
            return {}


class ProgressMonitor:
    def __init__(self, sources, output, *, poll_interval=90, report_interval=120, stall_seconds=600, ledger=None, controller_id=None):
        if min(poll_interval, report_interval, stall_seconds) <= 0:
            raise ValueError('intervals must be positive')
        self.sources, self.output = sources, Path(output).resolve()
        if isinstance(sources, LocalSources):
            # A worktree does not isolate runtime data: require separate output.
            if self.output == sources.root or self.output.is_relative_to(sources.root / '.pipeline') or self.output.is_relative_to(sources.root / 'applications'):
                raise ValueError('use a separate monitor output directory, outside business state')
        # 阻碍台账 is business state: it lives beside the batch data, not in the report output.
        self.ledger_path = sources.root / '.pipeline' / 'blockers.sqlite3' if isinstance(sources, LocalSources) else ':memory:'
        self.ledger = ledger
        self.controller_id = controller_id
        try:
            self._ensure_ledger()
        except (OSError, sqlite3.Error):
            self.ledger = None  # Same worker heartbeat will retry and diagnose through QueueHealth.
        self.poll_interval, self.report_interval, self.stall_seconds = poll_interval, report_interval, stall_seconds
        self.path = self.output / 'monitor.json'
        self._next_poll = self._next_report = 0.0
        self.runtime_errors = {}

    def _ensure_ledger(self):
        if self.ledger is None:
            self.ledger = BlockerLedger(self.ledger_path)
        self.ledger.bind(str(getattr(self.sources, 'manifest', 'replay')), self.controller_id)
        row = self.ledger.db.execute('SELECT controller FROM dq_scopes WHERE scope=?',(self.ledger.scope,)).fetchone()
        if row and self.controller_id is None:
            self.ledger.controller = row['controller']
        return self.ledger

    def _load(self):
        if not self.path.exists():
            return {'version': 1, 'cursors': {}, 'events': {}, 'model_call_attempts': 0}
        state = read_json(self.path)  # Corruption fails closed; never silently reset delivery dedup.
        if state.get('version') != 1:
            raise ValueError('unsupported monitor state')
        return state

    def _save(self, state):
        atomic_write(self.path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')

    def tick(self, *, force=False, now=None):
        mono = time.monotonic()
        if not force and mono < self._next_poll:
            return None
        now = now or stamp()
        self._ensure_ledger()
        with locked(self.output):
            state = self._load()
            manifest, jobs, errors = self.sources.collect(state['cursors'])
            runtime_errors = dict(self.runtime_errors)
            errors.extend(runtime_errors.values())
            binding = str(getattr(self.sources, 'manifest', 'replay'))
            if state.get('source') not in (None, binding):
                raise ValueError('output belongs to a different batch')
            state['source'] = binding
            state['queue_scheduler'] = 'program-v2'
            self.ledger.bind(binding)
            counts = dict(submitted=0, synced=0, closed=0, reserved=0, occupied=0, paused=0)
            for j in jobs:
                j['submission_complete'] = bool(j.get('submitted'))
                j['reserved'] = bool(not j.get('terminal') and not j['submission_complete'] and (j.get('user_submitted') or (j['owner'] and (not j['paused'] or j['blocked'] == 'submission_unknown' or j['turn_state'] not in {'ended', 'not_started'}))))
                counts['reserved'] += int(j['reserved'] and j['counted'])
                counts['occupied'] += int(bool(j['owner']) and j['turn_state'] not in {'ended', 'not_started'} and not (j['closed'] and j['turn_state'] == 'interrupted'))
                counts['paused'] += int(j['paused'])
                for field in ('submitted', 'synced', 'closed'):
                    counts[field] += int((j['submission_complete'] if field == 'submitted' else j[field]) and j['counted'])
                j['next_step'] = ('岗位已终止，保留原页，禁止重投' if j.get('terminal') else '完成，保留原页，禁止重投' if j['submission_complete'] and j['synced'] else '核查提交结果，禁止重投' if j.get('user_submitted') and not j['submitted'] else '补正式同步，保留原页，禁止重投' if j['submission_complete'] else '保留页面，不恢复' if j['paused'] else
                    '核查提交结果，禁止重投' if j['blocked'] == 'submission_unknown' else
                    '主控处理阻碍' if j['blocked'] or j['errors'] else '等待原任务证据' if j['owner'] else
                    '等待用户处理验证码' if j.get('held') else '等待现有 worker 调度')
                activity = j['last_tool_at'] or j.get('turn_at')
                if activity and j['turn_state'] == 'active':
                    try:
                        stalled = (datetime.fromisoformat(now) - datetime.fromisoformat(activity.replace('Z', '+00:00'))).total_seconds() >= self.stall_seconds
                    except ValueError:
                        stalled = False
                    if stalled:
                        j['errors'].append('no_recent_tool_activity_not_proof_of_stop')
                for reason in j['errors']:
                    errors.append(f"{j['job_id']}:{reason}")
            target = int(manifest['target_successful_submissions'])
            complete = counts['submitted'] >= target and counts['synced'] >= target and not counts['occupied'] and not any(j['blocked'] == 'submission_unknown' for j in jobs) and not errors
            gap = None
            if state.get('checked_at'):
                delta = (datetime.fromisoformat(now) - datetime.fromisoformat(state['checked_at'])).total_seconds()
                if delta > max(60, self.poll_interval * 3) or delta < 0:
                    gap = f"上次检查 {state['checked_at']}；间隔 {delta:.0f} 秒，期间未确认持续监控"
            requests = {}
            for j in jobs:
                start = j.get('first_started_at')
                end = now if j['turn_state'] == 'active' else j.get('turn_at')
                try:
                    j['elapsed_seconds'] = max(0, int((datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds())) if start and end else None
                except (ValueError, TypeError):
                    j['elapsed_seconds'] = None
                for response_id, usage in state['cursors'].get(j['owner'], {}).get('requests', {}).items():
                    requests[response_id] = usage
            usage = {k: sum(r[k] for r in requests.values()) for k in
                     ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
            usage.update(requests=len(requests), total_tokens=usage['input_tokens'] + usage['output_tokens'])
            # A local closeout or inactivity warning must remain visible without
            # halting unrelated jobs. Capacity still counts the original owner.
            # A Claude owner with no launch record never ran; the worker's sync launches it.
            local_warnings = {'close_evidence_missing', 'no_recent_tool_activity_not_proof_of_stop', 'claude_launch_missing'}
            dispatch_errors = [e for e in errors if e not in runtime_errors.values() and e.rsplit(':', 1)[-1] not in local_warnings
                and not any(e.startswith(str(j['job_id']) + ':') for j in jobs if not j['counted'])]
            snapshot = dict(membership_revision=manifest.get('membership_revision', 0), dispatch_errors=dispatch_errors, checked_at=now, target=target, counts=counts, jobs=jobs, errors=errors, gap=gap,
                            complete=complete, usage=usage, config_hash=digest(manifest), max_per_company=manifest.get('max_per_company'))
            if manifest.get('resume_mode') == 'selected_existing' and isinstance(self.sources, LocalSources):
                from pipeline.config import PipelineConfig
                snapshot['readiness'] = existing_resume_readiness(PipelineConfig.discover(self.sources.root), manifest.get('resume_catalog'))
            if isinstance(self.sources, LocalSources):
                from pipeline.coordination import CoordinationStore
                try:
                    attach_resources(snapshot, CoordinationStore(self.sources.root, scope=binding, jobfeed_db=self.sources.jobfeed_db).snapshot())
                except (OSError, ValueError, sqlite3.Error) as exc:
                    snapshot['coordination_error'] = type(exc).__name__
            def emit(kind, job_id, detail, version='', local=False):
                key = digest([kind, job_id, detail, version])
                if key in state['events']:
                    return
                state['events'][key] = dict(id=key, kind=kind, job_id=job_id, detail=detail, audience='controller',
                                            created_at=now, delivery='local' if local else 'pending')
            old = state.get('snapshot')
            # First scan reports existing facts locally, avoiding a burst of historical success notices.
            if old:
                previous = {j['job_id']: j for j in old['jobs']}
                for j in jobs:
                    before = previous.get(j['job_id'], {})
                    if j['submitted'] and not before.get('submitted'):
                        emit('新增提交成功', j['job_id'], '有岗位匹配回执；同步和关页分别核验', local=True)
                if complete and not old.get('complete'):
                    emit('整批完成', None, '提交、正式同步和关页已确认；停止派发，核验 worker 停止及 heartbeat 暂停', local=True)
            # No agent running while jobs wait for dispatch: report once per idle
            # episode after STALL_MINUTES, so a silent halt reaches the controller.
            queued = sum(j['next_step'] == '等待现有 worker 调度' for j in jobs)
            if counts['occupied'] == 0 and queued and not complete:
                idle_since = state.setdefault('idle_since', now)
                idle = (datetime.fromisoformat(now) - datetime.fromisoformat(idle_since)).total_seconds() / 60
                if idle >= STALL_MINUTES:
                    errors.append('system:dispatch_stalled_since:' + idle_since)
            else:
                state.pop('idle_since', None)
            # 阻碍台账 judges, dedupes and queues every blocker; the monitor only relays.
            # Job-scoped runtime errors ("7:claude_dispatch_error:...") belong to that job.
            scoped = {}
            for error in errors:
                head, _, reason = str(error).partition(':')
                if head.isdigit():
                    scoped.setdefault(int(head), []).append(reason)
            self.ledger.reconcile_membership([j['id'] for j in manifest.get('ordered_candidates', []) + manifest.get('carryover_candidates', [])] + [j['job_id'] for j in jobs if not j['counted']], now)
            facts = [{**j, 'errors': j['errors'] + [r for r in scoped.get(j['job_id'], []) if r not in j['errors']]} for j in jobs]
            for issue in self.ledger.observe(facts, [e for e in errors if not str(e).partition(':')[0].isdigit()], now):
                emit('待用户处理', issue['job_id'], handoff_message(issue), [issue['id'], issue['notified_at']])
            pending = self.ledger.pending_count()
            if not pending:
                state.pop('todo_wake_at', None)
                for event in state['events'].values():
                    if event['kind'] == '待办唤醒' and event['delivery'] == 'pending':
                        event.update(delivery='superseded', superseded_at=now, reason='queue_empty')
            else:
                wake_id = self.ledger.heartbeat_claim(now)
                if wake_id:
                    state['todo_wake_at'] = now
                    state['queue_wake_id'] = wake_id
                    detail = wake_message(pending, scope=binding, controller=self.ledger.controller)
                    emit('待办唤醒', None, detail, wake_id)
            snapshot['blockers'] = self.ledger.board()
            if str(self.ledger.path) != ':memory:':
                state['ledger'] = str(self.ledger.path)
            atomic_write(self.output / 'blockers.md', render_blockers(snapshot['blockers']))
            snapshot['pending_notifications'] = sum(e['delivery'] in {'pending', 'unknown'} for e in state['events'].values())
            state.update(snapshot=snapshot, checked_at=now)
            self._save(state)
            from pipeline.progress_report import render_dashboard
            atomic_write(self.output / 'dashboard.html', render_dashboard(snapshot))
            if force or mono >= self._next_report:
                atomic_write(self.output / 'latest.md', render_report(snapshot))
                self._next_report = mono + self.report_interval
            self._next_poll = mono + self.poll_interval
            return snapshot
