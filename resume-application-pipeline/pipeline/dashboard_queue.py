"""Store Dashboard queue selections, and start Claude Appliers only for an explicit handoff."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from pipeline.local_state import confined, dump, identifier, now, read_only
from pipeline.jobfeed_intake import JobfeedIntake

SCHEMA = '''CREATE TABLE IF NOT EXISTS preparation_requests(
 request_id TEXT PRIMARY KEY, selection TEXT NOT NULL, provenance TEXT NOT NULL,
 items TEXT NOT NULL, jobs TEXT NOT NULL, status TEXT NOT NULL, plan_id TEXT,
 scope TEXT, reason TEXT, created_at TEXT NOT NULL)'''
SETTLED = {'Ready for Review', 'Awaiting Approval', 'Needs Input', 'Needs Verification'}


def backend_origin(value):
    if not isinstance(value, str):
        raise ValueError('the verified dashboard backend origin is required')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('the dashboard backend must be a loopback origin')
    return parsed.scheme, parsed.port or (443 if parsed.scheme == 'https' else 80)


def dashboard_provenance(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'origin', 'received_at'} or value['kind'] != 'local_dashboard':
        raise ValueError('trusted local dashboard provenance is required')
    parsed = urlsplit(value['origin'])
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'localhost', '127.0.0.1', '::1', 'jobfeed.localhost'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('local dashboard origin is required')
    if not datetime.fromisoformat(value['received_at'].replace('Z', '+00:00')).tzinfo:
        raise ValueError('dashboard receipt time requires a timezone')
    return value


class DashboardQueue:
    def __init__(self, root, jobfeed_db, *, claude_host=None):
        self.root, self.jobfeed_db = Path(root).resolve(), Path(jobfeed_db).resolve()
        self.path = self.root / '.pipeline/dashboard-queue.sqlite3'
        self.claude_host = claude_host

    @contextmanager
    def db(self, write=False):
        confined(self.root, self.path)
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
            os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute(SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(preparation_requests)')}
            for name, default in (('intent', 'legacy_automatic'), ('dispatch_context', '{}'), ('observed_controller', '')):
                if name not in columns:
                    connection.execute(f"ALTER TABLE preparation_requests ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        else:
            connection = read_only(self.path)
        try:
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('request_id', 'status', 'plan_id', 'reason')} | {
            'status': 'unknown_delivery' if row['status'] == 'admitting' else row['status'],
            'intent': row['intent'] if 'intent' in row.keys() else 'legacy_automatic',
            'controller_thread_id': (row['observed_controller'] or None) if 'observed_controller' in row.keys() else None,
            'runtime': 'claude',
            'items': json.loads(row['items']), 'submit_policy': 'stop_before_submit'}

    def status(self, request_id=None, *, reason=None):
        if request_id is not None:
            identifier(request_id, 'request_id')
        if not self.path.exists():
            return {'requests': [], 'consumer': {'state': 'idle', 'reason': reason}}
        with self.db() as db:
            rows = db.execute('SELECT * FROM preparation_requests' + (' WHERE request_id=?' if request_id else '') + ' ORDER BY created_at DESC LIMIT 100', (request_id,) if request_id else ()).fetchall()
        receipts = []
        for row in rows:
            receipt = self.public(row)
            if receipt['intent'] == 'handoff':
                from pipeline.claude_dashboard_start import receipt as claude_receipt
                try:
                    receipt = claude_receipt(self, row, receipt, self.claude_host)
                except (ValueError, OSError, sqlite3.Error, KeyError) as error:
                    receipt.update(status='needs_attention', reason=str(error))
            receipts.append(receipt)
        return {'requests': receipts, 'consumer': {'state': 'idle', 'reason': reason}}

    def _save(self, request_id, **changes):
        allowed = {'items', 'jobs', 'status', 'plan_id', 'scope', 'reason', 'observed_controller'}
        if set(changes) - allowed:
            raise ValueError('invalid request update')
        with self.db(True) as db:
            db.execute('UPDATE preparation_requests SET ' + ','.join(key + '=?' for key in changes) + ' WHERE request_id=?',
                       [*(dump(value) if key in {'items', 'jobs'} else value for key, value in changes.items()), request_id])

    def enqueue(self, payload):
        if 'authorize_preparation' in payload:
            raise ValueError('queueing does not authorize preparation; use explicit controller handoff')
        return self._record(payload, 'queue')

    def handoff(self, payload):
        if payload.get('authorize_preparation') is not True:
            raise ValueError('explicit controller dispatch authorization is required')
        receipt = self._record(payload, 'handoff')
        self._dispatch(receipt['request_id'])
        return self.status(receipt['request_id'])['requests'][0]

    def _record(self, payload, intent):
        if not isinstance(payload, dict):
            raise ValueError('dashboard request must be an object')
        request_id = identifier(payload.get('request_id'), 'request_id')
        identifier('dashboard-' + request_id, 'dashboard request ID')
        items, rejected = payload.get('items'), payload.get('rejected_items', [])
        if (not isinstance(items, list) or not isinstance(rejected, list)
                or not 1 <= len(items) + len(rejected) <= 100):
            raise ValueError('authorize 1 to 100 exact preparation selections')
        provenance = dashboard_provenance(payload.get('provenance'))
        context = {}
        if intent == 'handoff':
            from pipeline.config import project_directory
            origin = payload.get('backend_origin')
            backend_origin(origin)
            context = {'backend_origin': origin, 'project_root': str(project_directory(self.root, payload.get('project_root')))}
            if payload.get('runtime', 'claude') != 'claude':
                raise ValueError('runtime must be claude: the Codex runtime was removed')
            context['runtime'] = 'claude'
        normalized = []
        for row in items:
            if (not isinstance(row, dict) or set(row) != {'real_job_id', 'source_job_id'}
                    or not isinstance(row['real_job_id'], str) or not row['real_job_id'].isdigit()
                    or int(row['real_job_id']) < 1 or type(row['source_job_id']) is not int or row['source_job_id'] < 1):
                raise ValueError('exact canonical and source job IDs are required')
            normalized.append(dict(row))
        rejected_rows = []
        for row in rejected:
            if (not isinstance(row, dict) or row.get('real_job_id') is not None and not isinstance(row.get('real_job_id'), str)
                    or not isinstance(row.get('reason'), str)
                    or row.get('source_job_id') is not None and (type(row['source_job_id']) is not int or row['source_job_id'] < 1)
                    or row.get('real_job_id') is None and row.get('source_job_id') is None):
                raise ValueError('invalid rejected selection')
            rejected_rows.append({key: row.get(key) for key in ('real_job_id', 'source_job_id', 'reason')} | {'status': 'rejected'})
        selection = normalized + [{key: row[key] for key in ('real_job_id', 'source_job_id')} for row in rejected_rows]
        if len({row['real_job_id'] for row in normalized}) != len(normalized) or len({(row['real_job_id'], row['source_job_id']) for row in selection}) != len(selection):
            raise ValueError('select each eligible canonical job and source only once')
        with self.db(True) as db:
            old = db.execute('SELECT * FROM preparation_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if json.loads(old['selection']) != selection or old['intent'] != intent or json.loads(old['dispatch_context']) != context:
                    raise ValueError('request ID already binds another selection')
                if old['status'] != 'admitting':
                    return self.public(old)
            else:
                pending = rejected_rows + [row | {'status': 'queued', 'reason': 'Queue admission is not yet confirmed.'} for row in normalized]
                db.execute('INSERT INTO preparation_requests(request_id,selection,provenance,items,jobs,status,plan_id,scope,reason,created_at,intent,dispatch_context) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?,?)',
                           (request_id, dump(selection), dump(provenance), dump(pending), '[]', 'admitting', now(), intent, dump(context)))
        self._admit(request_id, normalized, rejected_rows)
        return self.status(request_id)['requests'][0]

    def _admit(self, request_id, selected, results):
        jobs = []
        intake = JobfeedIntake(self.jobfeed_db, self.root.parent / 'jobfeed')
        for item in selected:
            try:
                with closing(read_only(self.jobfeed_db)) as db:
                    job = db.execute('SELECT id,real_job_id,company,title,url,jd_text FROM jobs WHERE id=?', (item['source_job_id'],)).fetchone()
                    if not job or str(job['real_job_id']) != item['real_job_id']:
                        raise ValueError('The selected source no longer belongs to this canonical job.')
                admission = intake.prioritize_jobs([item['source_job_id']])['results'][0]
                if admission['status'] not in {'queued', 'already_queued'} or admission.get('queue_state') != 'Queued':
                    raise ValueError(admission.get('reason') or 'This application already has an outcome or retained execution state.')
                if set(admission.get('identity_warnings', [])) - {'related_sources'}:
                    raise ValueError('Related source or identity evidence requires review before handoff.')
                jobs.append(dict(real_job_id=item['real_job_id'], source_job_id=item['source_job_id'], **{key: job[key] for key in ('company', 'title', 'url', 'jd_text')}))
                results.append(item | {'status': admission['status'], 'reason': None})
            except (ValueError, OSError, sqlite3.Error, KeyError) as error:
                results.append(item | {'status': 'rejected', 'reason': str(error)})
        self._save(request_id, items=results, jobs=jobs, status='queued' if jobs else 'needs_attention', reason=None if jobs else 'No selected jobs were eligible for preparation.')

    def _dispatch(self, request_id):
        lock_path = confined(self.root, self.root / '.pipeline/dashboard-handoff.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._save(request_id, reason='Another handoff is being processed. This request will not start automatically.')
                return
            with self.db() as db:
                row = db.execute('SELECT * FROM preparation_requests WHERE request_id=?', (request_id,)).fetchone()
            if row is None or row['intent'] != 'handoff' or row['status'] in {'admitting', 'needs_attention', 'ready_for_review'}:
                return
            try:
                from pipeline.claude_dashboard_start import start
                start(self, dict(row), self.claude_host)
            except (ValueError, OSError, sqlite3.Error, KeyError) as error:
                self._save(row['request_id'], reason=str(error))

def finished(jobs):
    blocked = any(job.get('queue_state') in {'Needs Input', 'Needs Verification'} for job in jobs)
    return dict(status='needs_attention' if blocked else 'ready_for_review', reason='Drafts are preserved. Review missing answers or verification before continuing.' if blocked else 'Preparation finished. Review the saved forms before any submission.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--jobfeed-db', type=Path, required=True)
    parser.add_argument('command', choices=['status', 'enqueue', 'handoff'])
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('dashboard request exceeds the size limit')
        payload = json.loads(raw or '{}')
        service = DashboardQueue(args.root, args.jobfeed_db)
        result = service.enqueue(payload) if args.command == 'enqueue' else service.handoff(payload) if args.command == 'handoff' else service.status(payload.get('request_id'))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(json.dumps({'error': str(error), 'code': 'unavailable' if isinstance(error, (OSError, sqlite3.Error)) else 'invalid'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
