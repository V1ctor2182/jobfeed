"""One owner turn may submit an authorized job while its batch stays stopped."""
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from pipeline.cancellation import CancellationStore
from pipeline.config import PipelineConfig
from pipeline.store import ApplicationQueue
from pipeline.application_rerun import stop_snapshot, worker_stopped, read_thread, host_turn_state
from pipeline.local_state import identifier, private_json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def context(config, scope, job, owner):
    scope = str(Path(scope).resolve())
    manifest_path = Path(scope)
    if not manifest_path.is_relative_to(config.resume_root):
        raise ValueError('release scope escapes runtime')
    manifest = json.loads(manifest_path.read_text())
    candidate = next((c for c in manifest['ordered_candidates'] if c['id'] == job), None)
    if candidate is None:
        raise ValueError('job outside manifest')
    handoff = json.loads(manifest_path.with_name(manifest_path.stem + '-handoff.json').read_text())
    if handoff.get('manifest') != scope or handoff.get('status') != 'stopped_by_user':
        raise ValueError('release requires a stopped batch')
    queue = ApplicationQueue(config.jobfeed_db)
    item = queue.get(job)
    if not item or item.codex_application_session_id != owner or queue.paused(job):
        raise ValueError('current unpaused original owner required')
    if item.state.value not in {'Applying', 'Needs Input', 'Needs Verification', 'Resume Ready', 'Retry Later'}:
        raise ValueError('submission state requires reconciliation')
    grant = queue.submit_authorization(job)
    if not grant or grant.get('authorized') is not True or grant['source_thread_id'] != manifest['controller_thread_id']:
        raise ValueError('current controller submission authorization required')
    folder = config.applications_root / item.application_folder_relpath
    if any((folder / name).exists() for name in ('submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json', 'submission-attempt.json', 'submission-result.json')) or (config.resume_root / '.pipeline/submission-closeouts' / f'{job}.json').exists():
        raise ValueError('existing submission evidence requires reconciliation')
    gate = CancellationStore(config.resume_root / '.pipeline/execution-gates.sqlite3')
    with gate._db() as db:
        binding = db.execute('SELECT * FROM execution_bindings WHERE owner=?', (owner,)).fetchone()
        if not binding or binding['scope'] != scope or binding['job_id'] != job:
            raise ValueError('execution owner binding mismatch')
        stops = stop_snapshot(db, scope, job)
    with sqlite3.connect(config.resume_root / '.pipeline/blockers.sqlite3') as db:
        row = db.execute('SELECT controller FROM dq_scopes WHERE scope=?', (scope,)).fetchone()
        member = db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (scope,str(job))).fetchone()
    if not row or row[0] != grant['source_thread_id'] or not member or member[0] != 'active':
        raise ValueError('controller or membership changed')
    bank = json.loads((config.resume_root / '.pipeline/answer-bank.json').read_text())
    applicable = []
    for entry in bank['entries']:
        version = next(v for v in entry['versions'] if v['version'] == entry['current_version'])
        s = version['scope']
        if s.get('kind') == 'job' and str(s.get('job_id')) == str(job) and s.get('company','').lower() == candidate['company'].lower():
            if version['state'] != 'approved' or not version.get('approval'):
                raise ValueError('current job answer is not approved')
            applicable.append(entry['id'])
    if not applicable:
        raise ValueError('approved job answers required')
    from pipeline.resume_catalog import selected_materials
    selected_materials(folder, job_id=job, scope=scope, owner=owner)
    return {'scope':scope, 'job':job, 'owner':owner, 'controller':grant['source_thread_id'],
            'project':manifest.get('desktop_project_root',str(config.resume_root)),
            'snapshot':fingerprint({'manifest':manifest,'handoff':handoff,'stops':stops,'grant':grant,'bank':bank})}


def path_for(config, owner):
    return config.resume_root / '.pipeline/submission-releases' / (identifier(owner,'owner') + '.json')


def grant_release(config, scope, job, owner, turn):
    with worker_stopped(config.resume_root):
        current = context(config, scope, job, owner)
        if os.environ.get('CODEX_THREAD_ID') != current['controller']:
            raise ValueError('only the authorized controller may issue a release')
        thread = read_thread(owner)
        if thread.get('id') != owner or thread.get('cwd') != current['project'] or host_turn_state(thread, turn, current['project']) != 'active':
            raise ValueError('exact live owner turn required')
        target = path_for(config, owner)
        if target.exists():
            old = json.loads(target.read_text())
            if old['turn'] == turn:
                if old['snapshot'] != current['snapshot']:
                    raise ValueError('release changed; reconcile instead of renewing')
                return old
        now = datetime.now(timezone.utc)
        release = current | {'turn':turn, 'created_at':now.isoformat(), 'expires_at':(now+timedelta(minutes=15)).isoformat()}
        private_json(target, release, config.resume_root)
        return release


def permits(config, owner, turn, *, scope=None, job=None):
    from pipeline.dashboard_submission import permits as dashboard_permits
    if dashboard_permits(config, owner, turn, scope=scope, job=job):
        return True
    try:
        release = json.loads(path_for(config, owner).read_text())
        if (scope is not None and release['scope'] != str(scope)) or (job is not None and release['job'] != job):
            return False
        if not turn or release['turn'] != turn or datetime.now(timezone.utc) >= datetime.fromisoformat(release['expires_at']):
            return False
        with worker_stopped(config.resume_root):
            current = context(config, release['scope'], release['job'], owner)
        if current['snapshot'] != release['snapshot']:
            return False
        thread = read_thread(owner)
        return thread.get('id') == owner and thread.get('cwd') == current['project'] and host_turn_state(thread, turn, current['project']) == 'active'
    except (OSError, ValueError, KeyError, StopIteration, sqlite3.Error):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--scope',required=True)
    parser.add_argument('--job',type=int,required=True)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--turn',required=True)
    args=parser.parse_args()
    print(json.dumps(grant_release(PipelineConfig.discover(args.root),args.scope,args.job,args.owner,args.turn),indent=2))


if __name__ == '__main__':
    main()
