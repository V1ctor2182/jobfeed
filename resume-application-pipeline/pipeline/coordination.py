"""Durable job resources. Codex remains the browser ownership enforcement boundary."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

KINDS = frozenset({'word', 'clipboard_foreground', 'account_mutation', 'native_auth', 'native_workflow'})
TAB_STATES = frozenset({'observed', 'preserved', 'needs_reconciliation', 'closed', 'missing'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')
_RESOURCE_NAMESPACE = "CASE WHEN kind IN ('account_mutation','native_auth') THEN 'account' ELSE kind END"
_SCHEMA = f'''
CREATE TABLE IF NOT EXISTS coordination_bindings(
 scope TEXT NOT NULL, job_id INTEGER NOT NULL, owner TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope,job_id));
CREATE TABLE IF NOT EXISTS coordination_tabs(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, job_id INTEGER NOT NULL, owner TEXT NOT NULL,
 browser_id TEXT NOT NULL, tab_id TEXT NOT NULL, opener_tab_id TEXT, origin TEXT,
 state TEXT NOT NULL, observed_at TEXT NOT NULL, observation_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS coordination_active_tab
 ON coordination_tabs(browser_id,tab_id) WHERE state != 'closed';
CREATE TABLE IF NOT EXISTS coordination_claims(
 claim_id TEXT PRIMARY KEY, scope TEXT NOT NULL, job_id INTEGER NOT NULL, owner TEXT NOT NULL,
 kind TEXT NOT NULL, resource_key TEXT NOT NULL, request_id TEXT NOT NULL,
 token_hash TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, evidence TEXT,
 UNIQUE(scope,job_id,request_id));
CREATE UNIQUE INDEX IF NOT EXISTS coordination_active_resource_namespace
 ON coordination_claims({_RESOURCE_NAMESPACE},resource_key) WHERE status != 'released';
CREATE TABLE IF NOT EXISTS coordination_accounts(
 account_ref TEXT PRIMARY KEY, profile_id TEXT NOT NULL, tenant_origin TEXT NOT NULL,
 account_alias TEXT NOT NULL, resource_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coordination_account_grants(
 scope TEXT NOT NULL, job_id INTEGER NOT NULL, account_ref TEXT NOT NULL,
 PRIMARY KEY(scope,job_id,account_ref));
CREATE TABLE IF NOT EXISTS coordination_waits(
 scope TEXT NOT NULL, job_id INTEGER NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
 resource_key TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(scope,job_id,request_id));
'''


def _id(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'{label} must be an opaque identifier')
    return value


def _job(value):
    if type(value) is not int or value <= 0:
        raise ValueError('job_id must be a positive integer')
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('owner token required')
    return hashlib.sha256(value.encode()).hexdigest()


def _origin(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError('tab URL must be a string')
    parsed = urlsplit(value)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname:
        raise ValueError('tab URL must use HTTP or HTTPS')
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    return f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')


def _tenant_origin(value):
    origin = _origin(value)
    if origin is None:
        raise ValueError('tenant origin required')
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('tenant must be an origin without credentials or paths')
    if parsed.port == (443 if parsed.scheme == 'https' else 80):
        origin = origin.rsplit(':', 1)[0]
    return origin


def _evidence(value, *, uncertain=False):
    if not isinstance(value, dict) or set(value) - {'status', 'observation_id', 'artifact_ref', 'reason', 'prior_turn_id'}:
        raise ValueError('structured evidence with supported fields required')
    clean = {k: _id(value[k], k) for k in ('observation_id', 'reason', 'prior_turn_id') if k in value}
    if 'artifact_ref' in value:
        ref = value['artifact_ref']
        if not isinstance(ref, str) or not ref or len(ref) > 1024:
            raise ValueError('artifact_ref must be a relative path')
        if Path(ref).is_absolute() or '..' in Path(ref).parts or '\x00' in ref:
            raise ValueError('artifact_ref must stay within the workspace')
        clean['artifact_ref'] = ref
    if uncertain:
        if 'reason' not in clean:
            raise ValueError('uncertain outcome requires a reason code')
    else:
        if value.get('status') not in {'completed', 'not_started', 'cancelled', 'verified_idle'} or 'observation_id' not in clean:
            raise ValueError('known outcome and observation_id required')
        clean['status'] = value['status']
    return clean


class CoordinationStore:
    def __init__(self, root, *, scope, jobfeed_db=None):
        self.root = Path(root).resolve()
        self.path = self.root / '.pipeline' / 'coordination.sqlite3'
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError('scope required')
        self.scope = scope
        self.jobfeed_db = Path(jobfeed_db).resolve() if jobfeed_db is not None else None

    @contextmanager
    def _read(self, path):
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _current_owner(self, job_id, owner):
        _job(job_id)
        _id(owner, 'owner')
        if self.jobfeed_db is None or not self.jobfeed_db.is_file():
            raise ValueError('current Jobfeed owner unavailable')
        with self._read(self.jobfeed_db) as db:
            row = db.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
        if row:
            if row[0] != owner:
                raise ValueError('current application owner mismatch')
            return 'application'
        pointer = self.root / '.pipeline' / 'submission-closeouts' / f'{job_id}.json'
        gates = self.root / '.pipeline' / 'execution-gates.sqlite3'
        try:
            if not pointer.resolve().is_relative_to(self.root) or pointer.stat().st_size > 65536:
                raise ValueError('cleanup owner record unavailable')
            closeout = json.loads(pointer.read_text())
            if not isinstance(closeout, dict) or type(closeout.get('job_id')) is not int or closeout.get('job_id') != job_id or closeout.get('thread_id') != owner:
                raise ValueError('cleanup owner record mismatch')
            with self._read(gates) as gate:
                binding = gate.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (owner,)).fetchone()
            if not binding or (binding['scope'], binding['job_id']) != (self.scope, job_id):
                raise ValueError('cleanup execution binding mismatch')
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ValueError('current application owner unavailable for cleanup') from exc
        return 'cleanup'

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript(_SCHEMA)
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _binding(self, db, job_id, owner, *, allow_cleanup=True):
        mode = self._current_owner(job_id, owner)
        if mode == 'cleanup' and not allow_cleanup:
            raise ValueError('cleanup-only owner cannot acquire resources')
        row = db.execute('SELECT * FROM coordination_bindings WHERE scope=? AND job_id=?', (self.scope, job_id)).fetchone()
        if not row or row['owner'] != owner:
            raise ValueError('job resource binding required')
        return row

    def _controller(self, controller_thread_id, job_ids):
        _id(controller_thread_id, 'controller_thread_id')
        path = self.root / '.pipeline' / 'blockers.sqlite3'
        if not path.is_file():
            raise ValueError('active scope controller unavailable')
        with self._read(path) as db:
            active = db.execute('SELECT 1 FROM dq_scopes WHERE scope=? AND controller=? AND active=1', (self.scope, controller_thread_id)).fetchone()
            if not active:
                raise ValueError('active scope controller required')
            for job_id in job_ids:
                if not db.execute('SELECT 1 FROM dq_jobs WHERE scope=? AND job=?', (self.scope, str(job_id))).fetchone():
                    raise ValueError('job is not a member of controller scope')

    def configure_account(self, controller_thread_id, *, profile_id, tenant_origin, account_alias, job_ids):
        profile_id = _id(profile_id, 'profile_id')
        account_alias = _id(account_alias, 'account_alias')
        tenant_origin = _tenant_origin(tenant_origin)
        if not isinstance(job_ids, list) or not job_ids:
            raise ValueError('nonempty job_ids list required')
        job_ids = sorted({_job(job_id) for job_id in job_ids})
        self._controller(controller_thread_id, job_ids)
        resource_key = hashlib.sha256(json.dumps([tenant_origin, account_alias], separators=(',', ':')).encode()).hexdigest()
        account_ref = 'account-' + hashlib.sha256(json.dumps([profile_id, tenant_origin, account_alias], separators=(',', ':')).encode()).hexdigest()
        with self._write() as db:
            self._controller(controller_thread_id, job_ids)
            db.execute('INSERT OR IGNORE INTO coordination_accounts VALUES(?,?,?,?,?)',
                       (account_ref, profile_id, tenant_origin, account_alias, resource_key))
            db.executemany('INSERT OR IGNORE INTO coordination_account_grants VALUES(?,?,?)',
                           [(self.scope, job_id, account_ref) for job_id in job_ids])
        return {'scope': self.scope, 'job_ids': job_ids, 'account_ref': account_ref,
                'profile_id': profile_id, 'tenant_origin': tenant_origin,
                'account_alias': account_alias, 'resource_key': resource_key}

    def _account_grant(self, db, job_id, resource_key):
        grant = db.execute('SELECT 1 FROM coordination_account_grants g JOIN coordination_accounts a ON a.account_ref=g.account_ref WHERE g.scope=? AND g.job_id=? AND a.resource_key=?',
                           (self.scope, job_id, resource_key)).fetchone()
        if not grant:
            raise ValueError('account resource is not registered for this job and scope')

    def bind(self, job_id, owner):
        self._current_owner(job_id, owner)
        with self._write() as db:
            mode = self._current_owner(job_id, owner)
            row = db.execute('SELECT * FROM coordination_bindings WHERE scope=? AND job_id=?', (self.scope, job_id)).fetchone()
            if row and row['owner'] != owner:
                # The queue names this owner, so the bound one was retired by a requeue. It hands over only with no
                # shared resource still claimed, as an adoption does.
                if mode != 'application' or db.execute("SELECT 1 FROM coordination_claims WHERE scope=? AND job_id=? AND status!='released'",
                                                       (self.scope, job_id)).fetchone():
                    raise ValueError('job already bound to another owner')
                db.execute("UPDATE coordination_tabs SET state='closed' WHERE scope=? AND job_id=? AND owner=? AND state!='closed'",
                           (self.scope, job_id, row['owner']))
                db.execute('UPDATE coordination_bindings SET owner=?,revision=revision+1 WHERE scope=? AND job_id=? AND owner=?',
                           (owner, self.scope, job_id, row['owner']))
            other = db.execute('SELECT * FROM coordination_bindings WHERE owner=?', (owner,)).fetchone()
            if other and (other['scope'], other['job_id']) != (self.scope, job_id):
                raise ValueError('owner already bound to another job')
            db.execute('INSERT OR IGNORE INTO coordination_bindings VALUES(?,?,?,0)', (self.scope, job_id, owner))
            return dict(db.execute('SELECT * FROM coordination_bindings WHERE scope=? AND job_id=?', (self.scope, job_id)).fetchone())

    def observe_tabs(self, job_id, owner, tabs, expected_revision):
        self._current_owner(job_id, owner)
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(tabs, list):
            raise ValueError('tabs and expected revision required')
        observations, seen = [], set()
        for tab in tabs:
            if not isinstance(tab, dict) or set(tab) - {'browser_id', 'tab_id', 'opener_tab_id', 'url', 'origin', 'state', 'observation_id'}:
                raise ValueError('unsupported tab record')
            browser, ident = _id(tab.get('browser_id'), 'browser_id'), _id(tab.get('tab_id'), 'tab_id')
            opener = _id(tab['opener_tab_id'], 'opener_tab_id') if tab.get('opener_tab_id') is not None else None
            state = tab.get('state', 'observed')
            if state not in TAB_STATES or (browser, ident) in seen or opener == ident:
                raise ValueError('invalid or duplicate tab observation')
            seen.add((browser, ident))
            observation = tab.get('observation_id')
            if observation is not None or state in {'missing', 'closed'}:
                observation = _id(observation, 'observation_id')
            observations.append((browser, ident, opener, _origin(tab.get('url', tab.get('origin'))), state, observation))
        with self._write() as db:
            binding = self._binding(db, job_id, owner)
            if binding['revision'] != expected_revision:
                raise ValueError('tab revision conflict')
            for browser, ident, opener, origin, state, observation in observations:
                current = db.execute("SELECT * FROM coordination_tabs WHERE browser_id=? AND tab_id=? AND state != 'closed'", (browser, ident)).fetchone()
                if current and (current['scope'], current['job_id'], current['owner']) != (self.scope, job_id, owner):
                    raise ValueError('tab belongs to another job')
                if opener:
                    parent = db.execute("SELECT * FROM coordination_tabs WHERE browser_id=? AND tab_id=? AND state != 'closed'", (browser, opener)).fetchone()
                    input_parent = next((t for t in observations if t[0:2] == (browser, opener)), None)
                    if input_parent and input_parent[4] in {'closed', 'missing'}:
                        raise ValueError('popup opener is closed or missing')
                    if not input_parent and (not parent or (parent['scope'], parent['job_id'], parent['owner']) != (self.scope, job_id, owner)):
                        raise ValueError('popup opener does not belong to this job')
                values = (opener, origin, state, _now(), observation)
                if current:
                    db.execute('UPDATE coordination_tabs SET opener_tab_id=?,origin=?,state=?,observed_at=?,observation_id=? WHERE id=?', values + (current['id'],))
                else:
                    if state in {'closed', 'missing'}:
                        raise ValueError('cannot close an unrecorded tab')
                    db.execute('INSERT INTO coordination_tabs VALUES(?,?,?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, self.scope, job_id, owner, browser, ident) + values)
            db.execute('UPDATE coordination_bindings SET revision=revision+1 WHERE scope=? AND job_id=?', (self.scope, job_id))
            return {'scope': self.scope, 'job_id': job_id, 'owner': owner, 'revision': expected_revision + 1,
                    'tabs': [dict(r) for r in db.execute('SELECT * FROM coordination_tabs WHERE scope=? AND job_id=? ORDER BY observed_at,id', (self.scope, job_id))]}

    @staticmethod
    def _public(row):
        result = dict(row)
        result.pop('token_hash', None)
        if result.get('evidence') is not None:
            result['evidence'] = json.loads(result['evidence'])
        return result

    def acquire(self, job_id, owner, kind, resource_key, request_id, owner_token):
        if self._current_owner(job_id, owner) == 'cleanup':
            raise ValueError('cleanup-only owner cannot acquire resources')
        if kind not in KINDS:
            raise ValueError('unsupported resource kind')
        _id(resource_key, 'resource_key')
        _id(request_id, 'request_id')
        token_hash = _token(owner_token)
        account_kind = kind in {'account_mutation', 'native_auth'}
        if account_kind:
            if not self.path.is_file():
                raise ValueError('account resource is not registered for this job and scope')
            with self._read(self.path) as db:
                self._account_grant(db, job_id, resource_key)
        with self._write() as db:
            self._binding(db, job_id, owner, allow_cleanup=False)
            if account_kind:
                self._account_grant(db, job_id, resource_key)
            row = db.execute('SELECT * FROM coordination_claims WHERE scope=? AND job_id=? AND request_id=?', (self.scope, job_id, request_id)).fetchone()
            if row:
                if (row['owner'], row['kind'], row['resource_key']) != (owner, kind, resource_key) or not hmac.compare_digest(row['token_hash'], token_hash):
                    raise ValueError('request replay does not match original claim')
                return self._public(row)
            waiting = db.execute('SELECT * FROM coordination_waits WHERE scope=? AND job_id=? AND request_id=?', (self.scope, job_id, request_id)).fetchone()
            if waiting and (waiting['owner'], waiting['kind'], waiting['resource_key']) != (owner, kind, resource_key):
                raise ValueError('waiting request replay mismatch')
            namespace = 'account' if kind in {'account_mutation', 'native_auth'} else kind
            busy = db.execute(f"SELECT * FROM coordination_claims WHERE {_RESOURCE_NAMESPACE}=? AND resource_key=? AND status!='released'", (namespace, resource_key)).fetchone()
            if busy:
                db.execute("INSERT OR IGNORE INTO coordination_waits VALUES(?,?,?,?,?,?,'waiting',?)", (self.scope, job_id, owner, kind, resource_key, request_id, _now()))
                return {'status': 'waiting', 'scope': self.scope, 'job_id': job_id, 'kind': kind,
                        'resource_key': resource_key, 'claim_id': busy['claim_id']}
            now, claim_id = _now(), uuid.uuid4().hex
            db.execute('INSERT INTO coordination_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (claim_id, self.scope, job_id, owner, kind, resource_key, request_id, token_hash, 'held', 0, now, now, None))
            db.execute("UPDATE coordination_waits SET status='acquired' WHERE scope=? AND job_id=? AND request_id=?", (self.scope, job_id, request_id))
            return self._public(db.execute('SELECT * FROM coordination_claims WHERE claim_id=?', (claim_id,)).fetchone())

    def _claim(self, db, claim_id):
        _id(claim_id, 'claim_id')
        row = db.execute('SELECT * FROM coordination_claims WHERE claim_id=? AND scope=?', (claim_id, self.scope)).fetchone()
        if not row:
            raise ValueError('claim not found in this scope')
        self._binding(db, row['job_id'], row['owner'])
        return row

    def _finish(self, claim_id, token, evidence, status):
        clean = _evidence(evidence, uncertain=status == 'needs_reconciliation')
        if status == 'released' and clean.get('status') == 'verified_idle':
            raise ValueError('verified idle requires controller reconciliation')
        encoded = json.dumps(clean, sort_keys=True)
        with self._write() as db:
            row = self._claim(db, claim_id)
            if not hmac.compare_digest(row['token_hash'], _token(token)):
                raise ValueError('claim token mismatch')
            if row['status'] == status:
                if row['evidence'] != encoded:
                    raise ValueError('completion evidence replay mismatch')
                return self._public(row)
            if row['status'] == 'released':
                raise ValueError('released claim cannot change')
            if row['status'] == 'needs_reconciliation':
                raise ValueError('uncertain claim requires controller reconciliation')
            db.execute('UPDATE coordination_claims SET status=?,revision=revision+1,evidence=?,updated_at=? WHERE claim_id=?', (status, encoded, _now(), claim_id))
            return self._public(db.execute('SELECT * FROM coordination_claims WHERE claim_id=?', (claim_id,)).fetchone())

    def release(self, claim_id, token, evidence):
        return self._finish(claim_id, token, evidence, 'released')

    def mark_uncertain(self, claim_id, token, evidence):
        return self._finish(claim_id, token, evidence, 'needs_reconciliation')

    def reconcile(self, claim_id, controller_thread_id, evidence, expected_revision):
        _id(controller_thread_id, 'controller_thread_id')
        if not isinstance(evidence, dict):
            raise ValueError('structured evidence required')
        owner_ack = evidence.get('owner_ack_token')
        clean = _evidence({k: v for k, v in evidence.items() if k != 'owner_ack_token'})
        if clean.get('status') != 'verified_idle' or not clean.get('artifact_ref'):
            raise ValueError('verified idle artifact required')
        with self._write() as db:
            row = self._claim(db, claim_id)
            if type(expected_revision) is not int:
                raise ValueError('claim revision conflict')
            with self._read(self.root / '.pipeline' / 'blockers.sqlite3') as control:
                active = control.execute('SELECT 1 FROM dq_scopes WHERE scope=? AND controller=? AND active=1', (self.scope, controller_thread_id)).fetchone()
                member = control.execute('SELECT 1 FROM dq_jobs WHERE scope=? AND job=?', (self.scope, str(row['job_id']))).fetchone()
            if not active or not member:
                raise ValueError('active scope controller and job membership required')
            if row['status'] == 'released':
                clean['controller_thread_id'] = controller_thread_id
                artifact_path = (self.root / clean['artifact_ref']).resolve()
                if not artifact_path.is_relative_to(self.root) or not artifact_path.is_file() or artifact_path.stat().st_size > 65536:
                    raise ValueError('idle artifact unavailable within workspace')
                clean['artifact_sha256'] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                if row['revision'] == expected_revision + 1 and row['evidence'] == json.dumps(clean, sort_keys=True):
                    return self._public(row)
                raise ValueError('reconciliation evidence replay mismatch')
            if row['revision'] != expected_revision:
                raise ValueError('claim revision conflict')
            if row['status'] not in {'held', 'needs_reconciliation'}:
                raise ValueError('only unresolved claims can be reconciled')
            artifact_path = (self.root / clean['artifact_ref']).resolve()
            if not artifact_path.is_relative_to(self.root) or not artifact_path.is_file() or artifact_path.stat().st_size > 65536:
                raise ValueError('idle artifact unavailable within workspace')
            artifact_bytes = artifact_path.read_bytes()
            artifact = json.loads(artifact_bytes)
            clean['artifact_sha256'] = hashlib.sha256(artifact_bytes).hexdigest()
            expected = {'scope': self.scope, 'job_id': row['job_id'], 'owner': row['owner'], 'claim_id': claim_id,
                        'observation_id': clean['observation_id'], 'resource_state': 'idle'}
            if not isinstance(artifact, dict) or any(artifact.get(k) != v for k, v in expected.items()):
                raise ValueError('idle artifact does not match claim')
            try:
                observed = datetime.fromisoformat(artifact['observed_at'])
                if observed.tzinfo is None or observed < datetime.fromisoformat(row['updated_at']):
                    raise ValueError('idle artifact predates uncertain claim')
            except (KeyError, TypeError) as exc:
                raise ValueError('idle observation time required') from exc
            if owner_ack is not None:
                if not hmac.compare_digest(row['token_hash'], _token(owner_ack)):
                    raise ValueError('owner idle acknowledgment mismatch')
            else:
                prior_turn = clean.get('prior_turn_id')
                if not prior_turn:
                    raise ValueError('owner acknowledgment or verified prior turn stop required')
                if artifact.get('prior_turn_id') != prior_turn:
                    raise ValueError('idle artifact prior turn does not match stopped owner turn')
                with self._read(self.root / '.pipeline' / 'execution-gates.sqlite3') as gate:
                    stopped = gate.execute("SELECT observed_at FROM execution_stops WHERE owner=? AND turn_id=? AND status IN ('completed','interrupted','failed','cancelled')", (row['owner'], prior_turn)).fetchone()
                if not stopped:
                    raise ValueError('prior owner turn stop is unverified')
                stopped_at = datetime.fromisoformat(stopped['observed_at'])
                if stopped_at.tzinfo is None or not datetime.fromisoformat(row['created_at']) <= stopped_at <= observed:
                    raise ValueError('prior owner turn stop is stale or later than idle observation')
            clean['controller_thread_id'] = controller_thread_id
            db.execute("UPDATE coordination_claims SET status='released',revision=revision+1,evidence=?,updated_at=? WHERE claim_id=?", (json.dumps(clean, sort_keys=True), _now(), claim_id))
            return self._public(db.execute('SELECT * FROM coordination_claims WHERE claim_id=?', (claim_id,)).fetchone())

    def snapshot(self, job_ids=None):
        result = {'scope': self.scope, 'bindings': [], 'tabs': [], 'claims': [], 'waits': [], 'accounts': []}
        if job_ids is not None:
            job_ids = list(job_ids)
            for ident in job_ids:
                _job(ident)
            if not job_ids:
                return result
        if not self.path.is_file():
            return result
        params, where = [self.scope], 'scope=?'
        if job_ids is not None:
            where += ' AND job_id IN (' + ','.join('?' for _ in job_ids) + ')'
            params += job_ids
        with self._read(self.path) as db:
            for name in ('bindings', 'tabs', 'claims', 'waits'):
                rows = db.execute(f'SELECT * FROM coordination_{name} WHERE {where} ORDER BY job_id', params).fetchall()
                result[name] = [self._public(r) if name == 'claims' else dict(r) for r in rows]
            account_where = 'g.scope=?'
            if job_ids is not None:
                account_where += ' AND g.job_id IN (' + ','.join('?' for _ in job_ids) + ')'
            result['accounts'] = [dict(r) for r in db.execute(
                f'SELECT g.scope,g.job_id,a.* FROM coordination_account_grants g JOIN coordination_accounts a ON a.account_ref=g.account_ref WHERE {account_where} ORDER BY g.job_id,a.account_ref', params)]
        return result
