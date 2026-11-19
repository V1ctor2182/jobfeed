"""Durable dispatch fence and official PreToolUse guardrail.

A hook is not a browser-RPC enforcement boundary: an admitted REPL cell and
specialized paths can continue. Cancellation never claims to retract in-flight
calls. No runtime configuration is installed merely by importing this module.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ExecutionCancelled(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class CancellationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS execution_bindings (
                    owner TEXT PRIMARY KEY, scope TEXT NOT NULL, job_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_scope_cancellations (
                    scope TEXT PRIMARY KEY, source_id TEXT NOT NULL, reason TEXT NOT NULL, received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_cancellations (
                    scope TEXT NOT NULL, job_id INTEGER NOT NULL, source_id TEXT NOT NULL,
                    reason TEXT NOT NULL, received_at TEXT NOT NULL,
                    PRIMARY KEY(scope,job_id));
                CREATE TABLE IF NOT EXISTS execution_user_updates (
                    source_id TEXT NOT NULL, scope TEXT NOT NULL, job_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, evidence TEXT NOT NULL, received_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,scope,job_id,kind));
                CREATE TABLE IF NOT EXISTS execution_stops (
                    owner TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL,
                    observed_at TEXT NOT NULL, PRIMARY KEY(owner,turn_id));
                CREATE TABLE IF NOT EXISTS application_reruns (
                    request_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, job_id INTEGER NOT NULL,
                    scope TEXT NOT NULL, controller TEXT NOT NULL, owner_thread_id TEXT NOT NULL,
                    jobfeed_db TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
                    turn_id TEXT, prompt TEXT NOT NULL, baseline TEXT NOT NULL, stops TEXT NOT NULL,
                    receipt TEXT, source TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS application_reruns_one_open
                    ON application_reruns(job_id) WHERE state IN ('unknown','queued','running');
                CREATE TABLE IF NOT EXISTS application_restarts (
                    request_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                    candidate_owner TEXT, target_turn TEXT, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_cleanup_turns (
                    owner TEXT NOT NULL, turn_id TEXT NOT NULL, action TEXT NOT NULL,
                    PRIMARY KEY(owner,turn_id));
            ''')

    @contextmanager
    def _db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            yield db

    def bind(self, scope, job_id, owner):
        if not scope or not owner:
            raise ValueError('scope and original owner required')
        with self._db() as db:
            old = db.execute('SELECT * FROM execution_bindings WHERE owner=?', (owner,)).fetchone()
            if old and (old['scope'], old['job_id']) != (scope, job_id):
                raise ValueError('owner already bound to another scope/job')
            db.execute('INSERT OR IGNORE INTO execution_bindings VALUES (?,?,?)', (owner, scope, job_id))

    def cancel(self, scope, job_id, source_id, reason='submitted_user_confirmed', *, received_at=None):
        if not source_id or not scope:
            raise ValueError('verified source and scope required')
        with self._db() as db:
            db.execute("UPDATE application_reruns SET state='blocked',reason='A newer stop revoked this rerun.' WHERE scope=? AND job_id=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason='The applier turn ended without completing.'))", (scope, job_id))
            db.execute('INSERT OR IGNORE INTO execution_cancellations VALUES (?,?,?,?,?)',
                       (scope, job_id, source_id, reason, received_at or utc_now()))
            # A later stop is more restrictive than a completion tombstone.
            if reason != 'submitted_user_confirmed':
                db.execute('UPDATE execution_cancellations SET reason=?,source_id=? WHERE scope=? AND job_id=?',
                           (reason, source_id, scope, job_id))
            return dict(db.execute('SELECT * FROM execution_cancellations WHERE scope=? AND job_id=?',
                                   (scope, job_id)).fetchone())

    def cancel_scope(self, scope, source_id, reason='stopped_by_user'):
        """Persist a non-resumable scope stop, including owners bound later."""
        if not scope or not source_id:
            raise ValueError('scope and stop source required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            received_at = utc_now()
            db.execute("UPDATE application_reruns SET state='blocked',reason='A newer stop revoked this rerun.' WHERE scope=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason='The applier turn ended without completing.'))", (scope,))
            db.execute('INSERT OR IGNORE INTO execution_scope_cancellations VALUES (?,?,?,?)',
                       (scope, source_id, reason, received_at))
            for row in db.execute('SELECT job_id FROM execution_bindings WHERE scope=?', (scope,)).fetchall():
                db.execute('INSERT INTO execution_cancellations VALUES (?,?,?,?,?) '
                           'ON CONFLICT(scope,job_id) DO UPDATE SET source_id=excluded.source_id,reason=excluded.reason',
                           (scope, row['job_id'], source_id, reason, received_at))
            return dict(db.execute('SELECT * FROM execution_scope_cancellations WHERE scope=?', (scope,)).fetchone())

    def receive_verified(self, evidence, kind):
        """Collector-only input; commit evidence and submission fence atomically."""
        source_id = evidence['source_thread_id'] + ':' + evidence['source_message_id']
        scope, job_id = evidence['scope'], evidence['job_id']
        encoded = json.dumps(evidence, sort_keys=True)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM execution_user_updates WHERE source_id=? AND scope=? AND job_id=? AND kind=?',
                                  (source_id, scope, job_id, kind)).fetchone()
            if previous and previous['evidence'] != encoded:
                raise ValueError('same source message cannot be rewritten')
            received_at = previous['received_at'] if previous else utc_now()
            db.execute('INSERT OR IGNORE INTO execution_user_updates VALUES (?,?,?,?,?,?)',
                       (source_id, scope, job_id, kind, encoded, received_at))
            if kind == 'submission':
                db.execute("UPDATE application_reruns SET state='blocked',reason='The user reported this application submitted.' WHERE scope=? AND job_id=? AND (state IN ('unknown','queued','running') OR (state='blocked' AND reason='The applier turn ended without completing.'))", (scope, job_id))
                db.execute('INSERT OR IGNORE INTO execution_cancellations VALUES (?,?,?,?,?)',
                           (scope, job_id, source_id, 'submitted_user_confirmed', received_at))
            return {'source_id': source_id, 'received_at': received_at, 'scope': scope, 'job_id': job_id}

    def verified_receipt(self, source_id, scope, job_id, kind):
        with self._db() as db:
            row = db.execute('SELECT * FROM execution_user_updates WHERE source_id=? AND scope=? AND job_id=? AND kind=?',
                             (source_id, scope, job_id, kind)).fetchone()
            return dict(row) if row else None

    def state(self, owner):
        with self._db() as db:
            scope_stop = db.execute('''SELECT s.*, b.job_id FROM execution_bindings b
                JOIN execution_scope_cancellations s ON b.scope=s.scope WHERE b.owner=?''', (owner,)).fetchone()
            if scope_stop:
                return dict(scope_stop)
            row = db.execute('''SELECT c.* FROM execution_bindings b
                JOIN execution_cancellations c ON b.scope=c.scope AND b.job_id=c.job_id
                WHERE b.owner=?''', (owner,)).fetchone()
            return dict(row) if row else None

    def assert_dispatch(self, owner, action='apply'):
        state = self.state(owner)
        if state and not (state['reason'] == 'submitted_user_confirmed' and action in {'verify', 'sync', 'closeout'}):
            raise ExecutionCancelled('original application cancelled: ' + state['reason'])

    def grant_cleanup_turn(self, owner, turn_id, action):
        if action not in {'verify', 'sync', 'closeout'}:
            raise ValueError('only explicit completion cleanup turns may be granted')
        self.assert_dispatch(owner, action)
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO execution_cleanup_turns VALUES (?,?,?)', (owner, turn_id, action))

    def record_stop(self, owner, turn_id, status):
        if status not in {'completed', 'interrupted', 'failed'}:
            return {'status': 'stop_unverified', 'owner': owner, 'turn_id': turn_id}
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO execution_stops VALUES (?,?,?,?)',
                       (owner, turn_id, status, utc_now()))
        return {'status': 'stop_verified', 'owner': owner, 'turn_id': turn_id, 'turn_status': status}

    def hook_decision(self, payload):
        if payload.get('hook_event_name') != 'PreToolUse':
            return {}
        owner = payload.get('session_id')
        if not isinstance(owner, str) or not owner:
            return deny_output('Execution gate requires runtime session identity.')
        state = self.state(owner)
        if not state:
            return {}
        from pipeline.submission_release import permits
        from pipeline.config import PipelineConfig
        if permits(PipelineConfig.discover(self.path.parent.parent), owner, payload.get('turn_id')):
            return {}
        with self._db() as db:
            cleanup = db.execute('SELECT action FROM execution_cleanup_turns WHERE owner=? AND turn_id=?',
                                 (owner, payload.get('turn_id'))).fetchone()
        if cleanup and state['reason'] == 'submitted_user_confirmed':
            return {}
        from pipeline.application_rerun import permits_turn
        if permits_turn(self, owner, payload.get('turn_id')):
            return {}
        from pipeline.application_restart import permits_turn as permits_restart
        if permits_restart(self, owner, payload.get('turn_id')):
            return {}
        return deny_output('Application execution cancelled; controller must verify in-flight results. ' + state['reason'])


def deny_output(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def hook_configuration(python, root, database):
    """Return reviewable configuration; caller installs only at rollout, not hot."""
    import shlex
    command = ' '.join(shlex.quote(str(x)) for x in (python, Path(root)/'pipeline/cancellation.py', '--database', database))
    return {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': command}]}]}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        # Missing/corrupt database is a failed guard, never permission to continue.
        if not Path(args.database).is_file():
            raise ValueError('execution gate database missing')
        with sqlite3.connect(f'file:{Path(args.database).absolute()}?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_bindings'").fetchone():
                raise ValueError('execution gate schema missing')
        if __package__ in (None, ''):
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        result = CancellationStore(args.database).hook_decision(payload)
    except Exception:
        # Command-hook errors may fail open: return a valid explicit denial.
        result = deny_output('Execution gate unavailable; internal verification required.')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
