"""Frozen existing resumes and authenticated original-owner selection."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

MODE = 'selected_existing'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def confined(root, path):
    root, path = Path(root).resolve(), Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError('resume material path escapes private directory')
    return path.resolve()


def read_asset(path, kind):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('resume asset must be an explicit regular file')
    data = path.read_bytes()
    if not data or (kind == 'pdf' and not data.startswith(b'%PDF-')):
        raise ValueError('invalid or empty resume asset')
    if kind == 'facts':
        facts = json.loads(data)
        if not isinstance(facts, dict) or facts.get('schemaVersion') != 1:
            raise ValueError('Autofill facts require schemaVersion 1')
    if kind == 'text':
        data.decode('utf-8')
    return data


def publish(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('wb') as stream:
        os.chmod(temp, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class ResumeCatalog:
    entries: tuple[dict, ...]
    source_sha256: str

    @classmethod
    def load(cls, path):
        path = Path(path)
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('resumes'), list):
            raise ValueError('resume catalog requires schema_version 1 and resumes')
        entries, ids = [], set()
        for entry in value['resumes']:
            if not isinstance(entry, dict):
                raise ValueError('resume catalog entries must be objects')
            ident = entry.get('id', '')
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', ident) or ident in ids:
                raise ValueError('resume IDs must be unique safe identifiers')
            ids.add(ident)
            if entry.get('kind') == 'master':
                continue
            if entry.get('available') is not True:
                continue
            if not isinstance(entry.get('label'), str) or not entry['label'].strip():
                raise ValueError('resume label required')
            assets = {}
            for kind, key in [('pdf', 'pdf'), ('facts', 'autofill'), ('text', 'text')]:
                if key not in entry:
                    if kind == 'pdf':
                        raise ValueError('resume PDF required')
                    continue
                source = Path(entry[key])
                source = source if source.is_absolute() else path.parent / source
                data = read_asset(source, kind)
                assets[kind] = {'source': str(source.absolute()), 'sha256': digest(data)}
            metadata = {key:entry[key] for key in ('kind', 'role', 'google_doc_url', 'page_count', 'pdf_export') if key in entry}
            if entry.get('kind') == 'variant':
                export = entry.get('pdf_export') or {}
                if (not entry.get('role') or not entry.get('google_doc_url') or entry.get('page_count') != 1
                    or export.get('source_google_doc_url') != entry['google_doc_url']
                    or export.get('stored_name') != entry.get('profile_upload')
                    or export.get('provenance') not in {'user_declared', 'observed_google_docs_export'}):
                    raise ValueError('application variant requires its recorded one-page Google Docs export')
            entries.append({'id': ident, 'label': entry['label'], 'description': str(entry.get('description', '')), 'assets': assets, **metadata})
        if not entries:
            raise ValueError('no explicitly available resumes in resume-catalog.json')
        return cls(tuple(entries), digest(path.read_bytes()))

    def freeze(self, root):
        root = Path(root).resolve()
        entries = []
        for entry in self.entries:
            assets = {}
            for kind, asset in entry['assets'].items():
                data = read_asset(asset['source'], kind)
                if digest(data) != asset['sha256']:
                    raise ValueError('resume source changed during freeze')
                destination = confined(root, root / '.pipeline/resume-assets' / asset['sha256'] / {'pdf':'resume.pdf', 'facts':'resume.autofill.json', 'text':'resume.txt'}[kind])
                if destination.exists():
                    if digest(destination.read_bytes()) != asset['sha256']:
                        raise ValueError('frozen resume asset changed')
                else:
                    publish(destination, data)
                assets[kind] = {**asset, 'path': str(destination.relative_to(root))}
            entries.append({**entry, 'assets': assets})
        return {'schema_version': 1, 'source_catalog_sha256': self.source_sha256, 'entries': entries}


def verify_catalog(root, snapshot, *, sources=False):
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1 or not snapshot.get('entries'):
        raise ValueError('frozen existing-resume catalog required')
    if sources and digest((Path(root) / 'resume-catalog.json').read_bytes()) != snapshot.get('source_catalog_sha256'):
        raise ValueError('resume catalog changed after plan review')
    ids = set()
    for entry in snapshot['entries']:
        if entry['id'] in ids or 'pdf' not in entry['assets']:
            raise ValueError('invalid frozen resume catalog')
        ids.add(entry['id'])
        for kind, asset in entry['assets'].items():
            path = confined(root, Path(root) / asset['path'])
            if digest(read_asset(path, kind)) != asset['sha256']:
                raise ValueError('frozen resume digest mismatch')
            if sources and digest(read_asset(asset['source'], kind)) != asset['sha256']:
                raise ValueError('resume source changed after plan review')
    return snapshot


def local_catalog(config):
    root = config.resume_root
    path = confined(root, root / '.pipeline/local-resume-catalog.json')
    with locked(path.with_suffix('.lock')):
        if path.exists():
            snapshot = json.loads(path.read_text())
            if snapshot.get('source_catalog_sha256') != digest((root / 'resume-catalog.json').read_bytes()):
                raise ValueError('local resume catalog changed; stop admission and reconcile a new catalog scope explicitly. Existing selected copies retain their frozen scope.')
            return verify_catalog(root, snapshot, sources=True)
        snapshot = ResumeCatalog.load(root / 'resume-catalog.json').freeze(root)
        publish(path, encoded(snapshot))
        return snapshot


def prepare_application(config, job, scope, catalog):
    """Bind the job's folder to this catalog. With no active selection, an earlier contract moves into history. jd.md stays, because earlier runs add to it."""
    verify_catalog(config.resume_root, catalog)
    folder = confined(config.applications_root, config.applications_root / ('existing-' + digest(str(scope).encode())[:12] + '-' + str(job.id)))
    contract = {'schema_version': 1, 'mode': MODE, 'job_id': job.id, 'scope': str(scope), 'catalog': catalog}
    with locked(confined(config.applications_root, folder / '.resume-selection.lock')):
        path = folder / 'resume-catalog.json'
        if path.exists() and json.loads(path.read_text()) != contract:
            if (folder / 'resume-source.json').exists():
                raise ValueError('job already prepared against another catalog')
            previous = path.read_bytes()
            publish(confined(folder, folder / 'resume-catalogs' / f'{digest(previous)}.json'), previous)
            publish(path, encoded(contract))
        if not path.exists():
            publish(folder / 'jd.md', f'Jobfeed Job ID: {job.id}\nCompany: {job.company}\nTitle: {job.title}\nURL: {job.url}\n\n{job.description}'.encode())
            publish(path, encoded(contract))
    return folder


def _bound(config, scope, job_id, owner):
    from pipeline.store import ApplicationQueue
    from pipeline.cancellation import CancellationStore
    item = ApplicationQueue(config.jobfeed_db).get(job_id)
    if item and item.codex_application_session_id and item.codex_application_session_id != owner:
        raise ValueError('job belongs to a different original owner')
    if not item or item.codex_application_session_id != owner or not item.application_folder_relpath:
        return None
    if ApplicationQueue(config.jobfeed_db).paused(job_id):
        raise ValueError('job is paused')
    from pipeline.worker_recovery import stopped
    if Path(scope).is_file() and stopped(scope):
        raise ValueError('batch is stopped')
    gate = CancellationStore(config.resume_root / '.pipeline/execution-gates.sqlite3')
    with gate._db() as db:
        row = db.execute('SELECT * FROM execution_bindings WHERE owner=?', (owner,)).fetchone()
    if not row or row['scope'] != scope or row['job_id'] != job_id:
        raise ValueError('resume selection requires original bound Desktop owner')
    gate.assert_dispatch(owner)
    ledger = config.resume_root / '.pipeline/blockers.sqlite3'
    if ledger.is_file():
        with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            active = db.execute('SELECT active FROM dq_scopes WHERE scope=?', (scope,)).fetchone()
            member = db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (scope, str(job_id))).fetchone()
        if (active and not active[0]) or (member and member[0] != 'active'):
            raise ValueError('resume selection scope or membership inactive')
    return item


def authoritative_catalog(root, scope):
    root = Path(root).resolve()
    if str(scope) == str(root):
        path = confined(root, root / '.pipeline/local-resume-catalog.json')
        return verify_catalog(root, json.loads(path.read_text()))
    path = Path(scope)
    if not path.is_file():
        raise ValueError('unknown resume catalog scope')
    data = json.loads(path.read_text())
    if data.get('bridge_managed'):
        raise ValueError('bridge-managed manifests came from the removed Codex batch bridge')
    if data.get('resume_mode') != MODE:
        raise ValueError('scope has no existing resume policy')
    return verify_catalog(root, data.get('resume_catalog'))


def validate_contract(folder, contract):
    root = folder.parent.parent
    expected = authoritative_catalog(root, contract['scope'])
    if contract['catalog'] != expected:
        raise ValueError('job catalog differs from authoritative scope')
    return expected


def select_resume(config, scope, job_id, owner, resume_id, reason, *, binding_wait=10):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('selection reason required')
    deadline = time.monotonic() + min(max(binding_wait, 0), 30)
    item = _bound(config, str(scope), job_id, owner)
    while item is None and time.monotonic() < deadline:
        time.sleep(0.1)
        item = _bound(config, str(scope), job_id, owner)
    if item is None:
        from pipeline.store import ApplicationQueue
        pending = ApplicationQueue(config.jobfeed_db).get(job_id)
        if pending and pending.application_folder_relpath:
            folder = confined(config.applications_root, config.applications_root / pending.application_folder_relpath)
            contract = json.loads((folder / 'resume-catalog.json').read_text())
            if contract.get('job_id') == job_id and contract.get('scope') == str(scope):
                publish(folder / 'resume-selection-wait.json', encoded({'job_id': job_id, 'scope': str(scope), 'owner': owner}))
                if _bound(config, str(scope), job_id, owner) is not None:
                    return select_resume(config, scope, job_id, owner, resume_id, reason, binding_wait=0)
        raise ValueError('original owner binding not yet acknowledged; dispatcher will schedule one selection-only continuation after acknowledgment. Do not upload.')
    folder = confined(config.applications_root, config.applications_root / item.application_folder_relpath)
    with locked(confined(folder, folder / '.resume-selection.lock')):
        contract = json.loads((folder / 'resume-catalog.json').read_text())
        if contract['job_id'] != job_id or contract['scope'] != str(scope):
            raise ValueError('foreign job or scope')
        validate_contract(folder, contract)
        pointer = folder / 'resume-source.json'
        if pointer.exists():
            selected = selected_materials(folder, job_id=job_id, scope=str(scope), owner=owner)
            if selected['resume_id'] != resume_id:
                raise ValueError('resume selection is immutable')
            return selected
        verify_catalog(config.resume_root, contract['catalog'])
        entry = next((e for e in contract['catalog']['entries'] if e['id'] == resume_id), None)
        if not entry:
            raise ValueError('resume ID outside frozen catalog')
        names = {'pdf': 'selected.pdf', 'facts': 'selected.autofill.json', 'text': 'selected.txt'}
        assets = {}
        for kind, asset in entry['assets'].items():
            data = read_asset(confined(config.resume_root, config.resume_root / asset['path']), kind)
            destination = confined(folder, folder / names[kind])
            publish(destination, data)
            assets[kind] = {'path': names[kind], 'sha256': digest(data)}
        if 'facts' not in assets:
            data = encoded({'schemaVersion': 1})
            publish(folder / names['facts'], data)
            assets['facts'] = {'path': names['facts'], 'sha256': digest(data), 'coverage': 'upload_only_no_resume_facts'}
        receipt = {'schema_version': 2, 'kind': MODE, 'job_id': job_id, 'scope': str(scope), 'owner': owner,
                   'resume_id': resume_id, 'label': entry['label'], 'reason': reason, 'assets': assets,
                   'catalog_sha256': digest(encoded(contract['catalog']))}
        publish(pointer, encoded(receipt))
        return selected_materials(folder, job_id=job_id, scope=str(scope), owner=owner)


def retire_selection(folder, record_id):
    """Move a retired Applier's resume selection into history, so the job's next Applier selects its resume again."""
    folder = Path(folder).resolve()
    with locked(confined(folder, folder / '.resume-selection.lock')):
        pointer = folder / 'resume-source.json'
        if not pointer.exists() or json.loads(pointer.read_text()).get('kind') != MODE:
            return
        history = confined(folder, folder / 'resume-requeues' / f'{record_id}.json')
        history.parent.mkdir(exist_ok=True)
        os.replace(pointer, history)


def selected_materials(folder, *, job_id=None, scope=None, owner=None):
    folder = Path(folder).resolve()
    value = json.loads(confined(folder, folder / 'resume-source.json').read_text())
    if value.get('kind') != MODE or value.get('schema_version') != 2:
        raise ValueError('selected existing-resume receipt required')
    for key, expected in [('job_id', job_id), ('scope', scope), ('owner', owner)]:
        if expected is not None and value.get(key) != expected:
            raise ValueError('resume selection ' + key + ' mismatch')
    root = folder.parent.parent
    gate_path = confined(root, root / '.pipeline/execution-gates.sqlite3')
    with closing(sqlite3.connect(gate_path.as_uri() + '?mode=ro', uri=True)) as db:
        binding = db.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (value['owner'],)).fetchone()
    if not binding or binding != (value['scope'], value['job_id']):
        raise ValueError('selected resume owner binding mismatch')
    from pipeline.config import PipelineConfig
    from pipeline.store import ApplicationQueue
    row = ApplicationQueue(PipelineConfig.discover(root).jobfeed_db).get(value['job_id'])
    if row and (row.codex_application_session_id != value['owner'] or row.application_folder_relpath != str(folder.relative_to(root / 'applications'))):
        raise ValueError('selected resume queue binding mismatch')
    return _selected_contents(folder, value)


def _selected_contents(folder, value):
    contract = json.loads(confined(folder, folder / 'resume-catalog.json').read_text())
    if value['job_id'] != contract['job_id'] or value['scope'] != contract['scope'] or value['catalog_sha256'] != digest(encoded(contract['catalog'])):
        raise ValueError('resume selection catalog mismatch')
    validate_contract(folder, contract)
    entry = next((e for e in contract['catalog']['entries'] if e['id'] == value['resume_id']), None)
    if not entry or value['assets']['pdf']['sha256'] != entry['assets']['pdf']['sha256']:
        raise ValueError('selected PDF outside approved catalog')
    expected_kinds = set(entry['assets']) | {'facts'}
    if set(value['assets']) != expected_kinds:
        raise ValueError('selected resume companions mismatch')
    if 'facts' not in entry['assets'] and value['assets']['facts']['sha256'] != digest(encoded({'schemaVersion': 1})):
        raise ValueError('neutral sidecar cannot invent resume facts')
    paths = {}
    for kind, asset in value['assets'].items():
        path = confined(folder, folder / asset['path'])
        if digest(read_asset(path, kind)) != asset['sha256']:
            raise ValueError('selected resume material changed')
        if kind in entry['assets'] and asset['sha256'] != entry['assets'][kind]['sha256']:
            raise ValueError('selected companion outside approved catalog')
        paths[kind] = str(path)
    return {**value, 'paths': paths}


def transfer_selection(config, scope, job_id, previous_owner, owner, request_id):
    from pipeline.application_restart import authorize_resume_transfer
    from pipeline.store import ApplicationQueue
    scope = str(scope)
    if str(uuid.UUID(request_id)) != request_id or not previous_owner or not owner or previous_owner == owner:
        raise ValueError('distinct restart owners and canonical request ID required')
    if not authorize_resume_transfer(config, scope, job_id, previous_owner, owner, request_id):
        raise ValueError('resume transfer requires an acknowledged application restart')
    item = ApplicationQueue(config.jobfeed_db).get(job_id)
    if not item or item.codex_application_session_id not in {previous_owner, owner} or not item.application_folder_relpath:
        raise ValueError('resume transfer queue owner mismatch')
    folder = confined(config.applications_root, config.applications_root / item.application_folder_relpath)
    with locked(confined(folder, folder / '.resume-selection.lock')):
        gate_path = confined(config.resume_root, config.resume_root / '.pipeline/execution-gates.sqlite3')
        with closing(sqlite3.connect(gate_path.as_uri() + '?mode=ro', uri=True)) as db:
            for bound_owner in (previous_owner, owner):
                binding = db.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (bound_owner,)).fetchone()
                if (bound_owner == previous_owner or binding is not None) and binding != (scope, job_id):
                    raise ValueError('resume transfer execution binding mismatch')
        pointer = confined(folder, folder / 'resume-source.json')
        current = json.loads(pointer.read_text())
        if current.get('kind') != MODE or current.get('schema_version') != 2:
            raise ValueError('selected existing-resume receipt required')
        if current.get('job_id') != job_id or current.get('scope') != scope:
            raise ValueError('resume transfer job or scope mismatch')
        history_path = confined(folder, folder / 'resume-restarts' / (request_id + '.json'))
        if history_path.exists():
            history = json.loads(history_path.read_text())
            if (history.get('request_id'), history.get('previous_owner'), history.get('owner')) != (request_id, previous_owner, owner):
                raise ValueError('resume transfer history mismatch')
            original = history['selection']
        else:
            if current.get('owner') != previous_owner:
                raise ValueError('resume transfer requires the previous selection owner')
            original = current
        if original.get('owner') != previous_owner:
            raise ValueError('resume transfer original owner mismatch')
        replacement = {**original, 'owner': owner}
        if current not in (original, replacement):
            raise ValueError('resume selection changed during restart')
        result = _selected_contents(folder, replacement)
        if not authorize_resume_transfer(config, scope, job_id, previous_owner, owner, request_id):
            raise ValueError('application restart no longer permits resume transfer')
        if not history_path.exists():
            publish(history_path, encoded({'request_id': request_id, 'previous_owner': previous_owner,
                                           'owner': owner, 'selection': original}))
        if current != replacement:
            publish(pointer, encoded(replacement))
        return result


def verify_selection(config, scope, job_id, owner, *, restart_request=None, turn_id=None, submission_release=False):
    if submission_release:
        from pipeline.submission_release import permits
        if restart_request or not permits(config, owner, turn_id, scope=scope, job=job_id):
            raise ValueError('live submission release required')
        from pipeline.store import ApplicationQueue
        item = ApplicationQueue(config.jobfeed_db).get(job_id)
        if not item or item.codex_application_session_id != owner:
            raise ValueError('submission release owner changed')
        return selected_materials(config.applications_root / item.application_folder_relpath,
                                  job_id=job_id, scope=str(scope), owner=owner)
    if bool(restart_request) != bool(turn_id):
        raise ValueError('restart verification requires both request ID and turn ID')
    if not restart_request:
        item = _bound(config, str(scope), job_id, owner)
    else:
        from pipeline.application_restart import permits_resume_verify
        from pipeline.store import ApplicationQueue
        if not permits_resume_verify(config, str(scope), job_id, owner, restart_request, turn_id):
            raise ValueError('resume verification requires the live restart preparation turn')
        item = ApplicationQueue(config.jobfeed_db).get(job_id)
        if not item or item.codex_application_session_id != owner or ApplicationQueue(config.jobfeed_db).paused(job_id):
            raise ValueError('restart resume owner is unavailable or paused')
    if item is None or not item.application_folder_relpath:
        raise ValueError('original owner required')
    result = selected_materials(config.applications_root / item.application_folder_relpath,
                                job_id=job_id, scope=str(scope), owner=owner)
    if restart_request and not permits_resume_verify(config, str(scope), job_id, owner, restart_request, turn_id):
        raise ValueError('application restart no longer permits resume verification')
    return result


def restore_selection(config, scope, job_id, previous_owner, owner, request_id):
    from pipeline.application_restart import authorize_resume_rollback
    from pipeline.store import ApplicationQueue
    scope = str(scope)
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError('canonical restart request ID required')
    if not authorize_resume_rollback(config, scope, job_id, previous_owner, owner, request_id):
        raise ValueError('resume restoration requires a revoked incomplete adoption')
    item = ApplicationQueue(config.jobfeed_db).get(job_id)
    if not item or item.codex_application_session_id != previous_owner or not item.application_folder_relpath:
        raise ValueError('resume restoration requires unchanged previous queue owner')
    folder = confined(config.applications_root, config.applications_root / item.application_folder_relpath)
    with locked(confined(folder, folder / '.resume-selection.lock')):
        history_path = confined(folder, folder / 'resume-restarts' / (request_id + '.json'))
        history = json.loads(history_path.read_text())
        if (history.get('request_id'), history.get('previous_owner'), history.get('owner')) != (request_id, previous_owner, owner):
            raise ValueError('resume restoration history mismatch')
        original = history['selection']
        if (original.get('kind'), original.get('schema_version'), original.get('scope'),
                original.get('job_id'), original.get('owner')) != (MODE, 2, scope, job_id, previous_owner):
            raise ValueError('resume restoration original receipt mismatch')
        pointer = confined(folder, folder / 'resume-source.json')
        current = json.loads(pointer.read_text())
        if current not in (original, {**original, 'owner': owner}):
            raise ValueError('resume selection changed after incomplete adoption')
        result = _selected_contents(folder, original)
        if not authorize_resume_rollback(config, scope, job_id, previous_owner, owner, request_id):
            raise ValueError('application restart no longer permits resume restoration')
        if current != original:
            publish(pointer, encoded(original))
        return result


def selection_command(config, scope, job_id):
    from pipeline.config import runtime_command
    return runtime_command(config.resume_root, [str(config.resume_root / '.venv/bin/python'), '-m', 'pipeline.resume_catalog', '--root', str(config.resume_root),
                       'select', '--scope', str(scope), '--job', str(job_id), '--owner', '<your original Codex thread ID>', '--resume', '<catalog ID>', '--reason', '<reason>'])


def main(argv=None):
    from pipeline.config import PipelineConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    select = commands.add_parser('select')
    for name in ('scope', 'owner', 'resume', 'reason'):
        select.add_argument('--' + name, required=True)
    select.add_argument('--job', type=int, required=True)
    verify = commands.add_parser('verify')
    for name in ('scope', 'owner'):
        verify.add_argument('--' + name, required=True)
    verify.add_argument('--job', type=int, required=True)
    verify.add_argument('--restart-request')
    verify.add_argument('--turn-id')
    verify.add_argument('--submission-release', action='store_true')
    args = parser.parse_args(argv)
    config = PipelineConfig.discover(args.root)
    if args.command == 'verify':
        result = verify_selection(config, args.scope, args.job, args.owner,
                                  restart_request=args.restart_request, turn_id=args.turn_id,
                                  submission_release=args.submission_release)
    else:
        result = select_resume(config, args.scope, args.job, args.owner, args.resume, args.reason)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
