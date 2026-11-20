"""Claude Applier runtime state: job ownership, human requests, replies, notes, tabs and readbacks."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

# Employer wording that means the posting is closed. Barclays 2026-10-16: "We are sorry this job post no longer exists."
# 2026-11-04: "Job not found. The job you requested was not found." and a posting's 404 page are closures too.
CLOSED = re.compile(r'\b(?:not (?:currently |longer )?accepting applications|no longer accepting applications|(?:job|position|posting|job post|requisition|role|opening) (?:is |has )?(?:no longer (?:available|exists|open|active)|been (?:closed|filled)|is closed|expired|been removed)|this (?:job|position|posting|role) (?:is )?(?:closed|expired|filled)'
                    r"|(?:job|position|posting|job post|requisition|role|opening|page|404)(?: you(?: are|'re)? (?:requested|looking for))? (?:(?:was|is) not|could not be|cannot be|can't be|not) found)\b"
                    r'|该岗位已停止招聘|该岗位不再接受申请|职位已关闭|该职位已不存在', re.I)
NO_SPONSORSHIP = re.compile(r'sponsor|work (?:authori[sz]ation|visa|permit)|\b(?:H-?1B|OPT|CPT|visa)\b|citizen|担保|签证', re.I)
REQUEST_KINDS = frozenset({'question', 'blocker'})


def read_only(path, timeout=10):
    """A read-only connection to a store that may hold a crashed write.

    A writer that died mid-write leaves a journal that a read-only connection cannot roll back, so every read fails.
    A normal connection rolls it back first. While a live writer still holds the file, that connection only reads.
    """
    path = Path(path)
    if Path(f'{path}-journal').exists():
        with closing(sqlite3.connect(path, timeout=timeout)) as db:
            db.execute('SELECT count(*) FROM sqlite_master').fetchone()
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)

BLOCKER_TYPES = frozenset({'missing_fact', 'broken_component', 'user_only_check', 'new_terms',
                           'tab_missing', 'browser_disconnected', 'page_changed', 'foreign_owner', 'submit_unknown', 'captcha',
                           'already_applied'})
SUBMIT_OUTCOMES = frozenset({'submitted', 'not_submitted', 'unknown'})
# Submit may be requested again only after a recorded outcome proves that nothing was submitted.
RETRYABLE_SUBMIT = frozenset({'not_submitted'})
# A Submit attempt whose outcome nobody saw. The user's standing rule (2026-11-18): when the form tab of such an attempt is
# lost and no confirmation email arrives, the job is applied again from the start by a new Applier, which first looks for
# an earlier application on the site. RETRY_ACTOR is that requeue's actor.
ATTEMPTED = frozenset({'requested', 'unknown'})
RETRY_ACTOR = 'worker: lost tab after Submit, no confirmation email'
ENDED_RETRY_ACTOR = 'worker: Applier ended after Submit, no confirmation email'
RETRY_ACTORS = frozenset({RETRY_ACTOR, ENDED_RETRY_ACTOR})
# apply_browser is the worker's own Done to a browser_disconnected blocker once the applying browser runs again.
REPLY_SOURCES = frozenset({'discord', 'dashboard', 'email', 'no_email', 'apply_browser'})
# The user confirms Submit in the Dashboard or in Discord, or turned on auto submit mode; tests use 'test'.
SUBMIT_SOURCES = frozenset({'dashboard', 'discord', 'auto', 'test'})
AUTO_SUBMIT = 'auto_submit'
AUTO_SUBMIT_FIELDS = ('enabled_at', 'enabled_by', 'source')
AUTOPILOT = 'autopilot'
AUTOPILOT_FIELDS = ('changed_at', 'changed_by', 'source')
# A reply answers its question, or asks the Applier to rewrite the question's draft.
REPLY_INTENTS = frozenset({'answer', 'change_request'})
DRAFT_FEEDBACK_LIMIT = 20
DEFAULT_MODEL, STRONG_MODEL = 'sonnet', 'opus'
MAX_TEXT = 16384
# A draft must fit in one Discord message, so the user sees all of it before approving it.
MAX_DRAFT = 1200
# A question's fixed choices each become a Discord button: at most four rows of five, labels at most 80 characters.
MAX_OPTIONS, MAX_OPTION = 20, 80
IGNORE_REASON = 'Pressed Ignore in Discord'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS applier_jobs(
 job_id INTEGER PRIMARY KEY, owner TEXT NOT NULL UNIQUE, scope TEXT NOT NULL, model TEXT NOT NULL,
 created_at TEXT NOT NULL, last_progress_at TEXT, last_progress TEXT);
CREATE TABLE IF NOT EXISTS applier_requests(
 id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
 blocker_type TEXT, field TEXT, text TEXT NOT NULL, evidence TEXT, state TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS applier_live_request
 ON applier_requests(job_id,fingerprint) WHERE state IN ('open','answered');
CREATE TABLE IF NOT EXISTS applier_replies(
 request_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE, sender_id TEXT NOT NULL,
 received_at TEXT NOT NULL, PRIMARY KEY(request_id,version));
CREATE TABLE IF NOT EXISTS applier_notes(
 id TEXT PRIMARY KEY, site TEXT NOT NULL, widget TEXT NOT NULL, text TEXT NOT NULL,
 job_id INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applier_tabs(
 job_id INTEGER NOT NULL, tab_id TEXT NOT NULL, url TEXT NOT NULL, origin TEXT NOT NULL,
 recorded_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'observed', PRIMARY KEY(job_id,tab_id));
CREATE TABLE IF NOT EXISTS applier_readbacks(
 id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
 request_id TEXT, reply_version INTEGER, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applier_resumes(
 job_id INTEGER PRIMARY KEY, resume_id TEXT NOT NULL, label TEXT NOT NULL, upload_name TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applier_submit_auth(
 event_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, source_kind TEXT NOT NULL, recorded_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS applier_submits(
 job_id INTEGER PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL, requested_at TEXT NOT NULL,
 outcome_at TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS applier_outbox(
 job_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, created_at TEXT NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS applier_skips(
 job_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, sender_id TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applier_messages(
 id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
 delivered_at TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS applier_control(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applier_alerts(
 job_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
 sent_at TEXT, PRIMARY KEY(job_id,kind,detail));
CREATE TABLE IF NOT EXISTS applier_requeues(
 id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, previous_state TEXT NOT NULL, previous_folder TEXT, retired_owner TEXT,
 batches TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, requeued_at TEXT NOT NULL, retired TEXT NOT NULL);
'''
# An Applier's own records for its job. A new Applier for the job must start without them.
RUN_TABLES = ('applier_jobs', 'applier_launches', 'applier_tabs', 'applier_readbacks', 'applier_resumes')
SUBMIT_TABLES = ('applier_submit_auth', 'applier_submits', 'applier_outbox', 'applier_skips')
LIVE_REQUESTS = ('open', 'answered', 'escalated', 'escalation_sent')


def _id(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'{label} must be an opaque identifier')
    return value


def _job(value):
    if type(value) is not int or value <= 0:
        raise ValueError('job_id must be a positive integer')
    return value


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > MAX_TEXT:
        raise ValueError(f'{label} must be text of at most {MAX_TEXT} characters')
    return value


def _origin(url):
    parts = urlsplit(_text(url, 'url'))
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('url must be an http(s) page address')
    return f'{parts.scheme}://{parts.netloc.lower()}'


def _now():
    return datetime.now(timezone.utc).isoformat()


class ApplierRuntime:
    """One store for every Claude Applier. Each method commits before it returns."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / '.pipeline' / 'applier-runtime.sqlite3'

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript(_SCHEMA)
            if 'state' not in {row[1] for row in db.execute('PRAGMA table_info(applier_tabs)')}:
                db.execute("ALTER TABLE applier_tabs ADD COLUMN state TEXT NOT NULL DEFAULT 'observed'")
            columns = {row[1] for row in db.execute('PRAGMA table_info(applier_requests)')}
            if 'draft' not in columns:
                db.execute('ALTER TABLE applier_requests ADD COLUMN draft TEXT')
            if 'options' not in columns:
                db.execute('ALTER TABLE applier_requests ADD COLUMN options TEXT')
            if 'company' not in columns:
                db.execute('ALTER TABLE applier_requests ADD COLUMN company TEXT')
            if 'intent' not in {row[1] for row in db.execute('PRAGMA table_info(applier_replies)')}:
                db.execute('ALTER TABLE applier_replies ADD COLUMN intent TEXT')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _owned(self, db, job_id, owner):
        row = db.execute('SELECT owner FROM applier_jobs WHERE job_id=?', (_job(job_id),)).fetchone()
        if row is None or row['owner'] != _id(owner, 'owner'):
            raise ValueError('this session does not own the job')

    def bind(self, job_id, owner, scope, model=DEFAULT_MODEL):
        _job(job_id), _id(owner, 'owner'), _text(scope, 'scope'), _text(model, 'model')
        with self._write() as db:
            row = db.execute('SELECT owner,scope FROM applier_jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is not None:
                if (row['owner'], row['scope']) != (owner, scope):
                    raise ValueError('job already has an owner')
                return
            if db.execute('SELECT 1 FROM applier_jobs WHERE owner=?', (owner,)).fetchone():
                raise ValueError('owner already holds another job')
            db.execute('INSERT INTO applier_jobs(job_id,owner,scope,model,created_at) VALUES(?,?,?,?,?)', (job_id, owner, scope, model, _now()))

    def ask(self, job_id, owner, kind, text, *, field=None, blocker_type=None, evidence=None, draft=None, options=None, company=None):
        """Open one request. A question may carry the Applier's draft answer, which the user approves, replaces or asks
        to change, and the form's fixed choices, which the user picks from.

        Asking again returns the same request, so a restarted Applier gets its saved reply. Once the user asked for changes
        to the draft, asking again with a different draft closes that request as redrafted and opens a new one."""
        if kind not in REQUEST_KINDS:
            raise ValueError('unknown request kind')
        if (kind == 'blocker') != (blocker_type is not None) or (blocker_type is not None and blocker_type not in BLOCKER_TYPES):
            raise ValueError('a blocker needs exactly one known blocker_type')
        _text(text, 'text')
        if field is not None:
            _text(field, 'field')
        if draft is not None:
            if kind != 'question':
                raise ValueError('only a question can carry a draft answer')
            _text(draft, 'draft')
            if len(draft) > MAX_DRAFT:
                raise ValueError(f'a draft answer must be at most {MAX_DRAFT} characters; shorten it or ask without one')
        if options is not None:
            if kind != 'question':
                raise ValueError('only a question can offer options')
            options = [option.strip() for option in options]
            if not 2 <= len(options) <= MAX_OPTIONS or len(set(options)) != len(options) or not all(0 < len(option) <= MAX_OPTION for option in options):
                raise ValueError(f'options must be 2 to {MAX_OPTIONS} different choices of at most {MAX_OPTION} characters each; ask without options otherwise')
        evidence_json = None if evidence is None else json.dumps(evidence, sort_keys=True, ensure_ascii=False)
        if evidence_json is not None and len(evidence_json) > MAX_TEXT:
            raise ValueError('evidence is too large')
        fingerprint = hashlib.sha256(json.dumps([kind, blocker_type, field, ' '.join(text.split())]).encode()).hexdigest()
        with self._write() as db:
            self._owned(db, job_id, owner)
            row = db.execute("SELECT id,state,draft FROM applier_requests WHERE job_id=? AND fingerprint=? AND state IN ('open','answered')", (job_id, fingerprint)).fetchone()
            if row is not None:
                latest = db.execute('SELECT intent FROM applier_replies WHERE request_id=? ORDER BY version DESC LIMIT 1', (row['id'],)).fetchone()
                if row['state'] != 'answered' or latest['intent'] != 'change_request' or row['draft'] == draft:
                    return row['id']
                db.execute("UPDATE applier_requests SET state='redrafted' WHERE id=?", (row['id'],))
            state = 'open'
            if blocker_type == 'broken_component' and field is not None:
                model = db.execute('SELECT model FROM applier_jobs WHERE job_id=?', (job_id,)).fetchone()['model']
                tried = db.execute("SELECT 1 FROM applier_requests WHERE job_id=? AND field=? AND state IN ('escalated','escalation_sent')", (job_id, field)).fetchone()
                if model != STRONG_MODEL and tried is None:
                    state = 'escalated'
            request_id = uuid.uuid4().hex
            db.execute('INSERT INTO applier_requests(id,job_id,kind,fingerprint,blocker_type,field,text,evidence,state,created_at,draft,options,company) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id, job_id, kind, fingerprint, blocker_type, field, text, evidence_json, state, _now(), draft,
                        None if options is None else json.dumps(options, ensure_ascii=False), company))
            return request_id

    def request(self, request_id):
        rows = self._read('SELECT * FROM applier_requests WHERE id=?', (_id(request_id, 'request_id'),))
        if not rows:
            raise ValueError('unknown request')
        row = rows[0]
        return dict(row, evidence=json.loads(row['evidence']) if row['evidence'] else None,
                    options=json.loads(row['options']) if row['options'] else None)

    def escalations(self):
        """Blockers waiting for a restart on the stronger model, oldest first."""
        return self._read("SELECT id,job_id,field,text,evidence FROM applier_requests WHERE state='escalated' ORDER BY created_at", ())

    def escalation_sent(self, request_id, model=STRONG_MODEL):
        with self._write() as db:
            row = db.execute("SELECT job_id FROM applier_requests WHERE id=? AND state='escalated'", (request_id,)).fetchone()
            if row is None:
                raise ValueError('no pending escalation for that request')
            db.execute("UPDATE applier_requests SET state='escalation_sent' WHERE id=?", (request_id,))
            db.execute('UPDATE applier_jobs SET model=? WHERE job_id=?', (model, row['job_id']))

    def jobs(self):
        return self._read('SELECT job_id,owner,scope,model,last_progress,last_progress_at FROM applier_jobs ORDER BY job_id', ())

    def authorize_submit(self, job_id, *, source_kind, event_id):
        """Record one Submit authorization. Only the confirmation flow (Dashboard, Discord or auto submit mode) or a test calls this, never a model."""
        if source_kind not in SUBMIT_SOURCES:
            raise ValueError('submit authorization must come from the dashboard, discord or auto submit mode')
        _job(job_id), _id(event_id, 'event_id')
        with self._write() as db:
            if db.execute('SELECT 1 FROM applier_jobs WHERE job_id=?', (job_id,)).fetchone() is None:
                raise ValueError('unknown job')
            db.execute('INSERT OR IGNORE INTO applier_submit_auth(event_id,job_id,source_kind,recorded_at) VALUES(?,?,?,?)', (event_id, job_id, source_kind, _now()))

    def revoke_submit_auth(self, job_id, event_id):
        """Withdraw an authorization that was never used, for a confirmation that failed."""
        with self._write() as db:
            db.execute('DELETE FROM applier_submit_auth WHERE event_id=? AND job_id=? AND used_at IS NULL', (event_id, job_id))

    def request_submit(self, job_id, owner):
        """Persist one Submit attempt before the click, spending one unused authorization. Returns (allowed, reason)."""
        with self._write() as db:
            self._owned(db, job_id, owner)
            prior = db.execute('SELECT state,attempts FROM applier_submits WHERE job_id=?', (job_id,)).fetchone()
            if prior is not None and prior['state'] not in RETRYABLE_SUBMIT:
                return False, f"already_{prior['state']}"
            if db.execute('SELECT 1 FROM applier_skips WHERE job_id=?', (job_id,)).fetchone() is not None:
                return False, 'skipped'
            # Each authorization covers exactly one attempt.
            auth = db.execute('SELECT event_id FROM applier_submit_auth WHERE job_id=? AND used_at IS NULL ORDER BY recorded_at LIMIT 1', (job_id,)).fetchone()
            if auth is None:
                return False, 'not_authorized'
            db.execute('UPDATE applier_submit_auth SET used_at=? WHERE event_id=?', (_now(), auth['event_id']))
            if prior is None:
                db.execute("INSERT INTO applier_submits VALUES(?,'requested',1,?,NULL,NULL)", (job_id, _now()))
            else:
                db.execute("UPDATE applier_submits SET state='requested',attempts=attempts+1,requested_at=?,outcome_at=NULL WHERE job_id=?", (_now(), job_id))
            return True, 'authorized'

    def record_submit_outcome(self, job_id, owner, outcome, evidence):
        if outcome not in SUBMIT_OUTCOMES:
            raise ValueError('unknown submit outcome')
        _text(evidence, 'evidence')
        with self._write() as db:
            self._owned(db, job_id, owner)
            row = db.execute('SELECT state FROM applier_submits WHERE job_id=?', (job_id,)).fetchone()
            # A confirmation found after an unknown outcome settles it as submitted. Nothing else reopens an attempt.
            if row is None or not (row['state'] == 'requested' or (row['state'] == 'unknown' and outcome == 'submitted')):
                raise ValueError('record an outcome only right after an authorized request_submit')
            db.execute('UPDATE applier_submits SET state=?,outcome_at=?,evidence=? WHERE job_id=?', (outcome, _now(), evidence, job_id))
            if outcome == 'submitted':
                db.execute("INSERT OR IGNORE INTO applier_outbox(job_id,kind,state,created_at) VALUES(?,'jobfeed_sync','pending',?)", (job_id, _now()))
            return outcome

    def record_skip(self, job_id, owner, request_id, reply_version):
        """Record that the user's reply told this Applier to skip the posting. The worker then stops it, cancels the job and marks it ignored."""
        with self._write() as db:
            self._owned(db, job_id, owner)
            reply = db.execute('SELECT p.text,p.source_kind,p.source_id,p.sender_id FROM applier_replies p JOIN applier_requests r ON r.id=p.request_id '
                               'WHERE p.request_id=? AND p.version=? AND r.job_id=?', (request_id, reply_version, job_id)).fetchone()
            if reply is None:
                raise ValueError('a skip needs the request_id and reply_version of the user reply that asked for it')
            self._skip(db, job_id, reply['text'], reply['source_kind'], reply['source_id'], reply['sender_id'])

    def record_closed(self, job_id, owner, evidence):
        """Record the Applier's finding that the employer closed the posting. The worker then stops it and marks the job unavailable."""
        _text(evidence, 'evidence')
        if not CLOSED.search(evidence):
            raise ValueError('quote the exact page sentence that says the posting is closed, expired, filled or no longer accepting applications; '
                             'if the page does not say so, ask the user instead')
        with self._write() as db:
            self._owned(db, job_id, owner)
            self._skip(db, job_id, evidence, 'posting', owner, 'applier', outbox='posting_closed')

    def record_no_sponsorship(self, job_id, owner, evidence):
        """Record the Applier's finding that the employer does not sponsor. By the user's standing rule the job is then skipped like an ignored one."""
        _text(evidence, 'evidence')
        if not NO_SPONSORSHIP.search(evidence):
            raise ValueError('quote the exact sentence from the posting or form that says the employer does not sponsor; '
                             'a question asking whether you need sponsorship does not count')
        with self._write() as db:
            self._owned(db, job_id, owner)
            self._skip(db, job_id, f'No sponsorship: {evidence}'[:1000], 'posting', owner, 'applier')

    def ignore(self, job_id, *, source_id, sender_id, source_kind='discord', reason=IGNORE_REASON):
        """Record the user's own Ignore press in Discord or the Dashboard, with no Applier involved."""
        _id(source_id, 'source_id'), _id(sender_id, 'sender_id')
        if source_kind not in REPLY_SOURCES:
            raise ValueError('unknown reply source')
        with self._write() as db:
            if db.execute('SELECT 1 FROM applier_jobs WHERE job_id=?', (_job(job_id),)).fetchone() is None:
                raise ValueError('unknown job')
            self._skip(db, job_id, reason, source_kind, source_id, sender_id)

    def _skip(self, db, job_id, reason, source_kind, source_id, sender_id, outbox='user_skip'):
        submit = db.execute('SELECT state FROM applier_submits WHERE job_id=?', (job_id,)).fetchone()
        if submit is not None and submit['state'] not in RETRYABLE_SUBMIT:
            raise ValueError('Submit was already requested for this job, so it cannot be skipped')
        db.execute('INSERT OR IGNORE INTO applier_skips VALUES(?,?,?,?,?,?)', (job_id, reason, source_kind, source_id, sender_id, _now()))
        # Nothing should wait on a question about a job the user dropped.
        db.execute("UPDATE applier_requests SET state='withdrawn' WHERE job_id=? AND state='open'", (job_id,))
        db.execute("INSERT OR IGNORE INTO applier_outbox(job_id,kind,state,created_at) VALUES(?,?,'pending',?)", (job_id, outbox, _now()))

    @staticmethod
    def requeue_id(job_id, previous_state, previous_folder, owner):
        return f"{job_id}:{previous_state}:{previous_folder or '-'}:{owner or '-'}"

    @staticmethod
    def _settled_unsent(submit, approvals, unknown_sent_nothing=False, retry_attempt=False):
        """Whether a job's Submit records sent nothing, with exactly these approvals: no attempt is open, and request_submit
        opens one in the same write that spends an approval, so each approval was spent on a finished attempt or never."""
        return (not submit['applier_outbox'] and not submit['applier_skips']
                and all(row['state'] in RETRYABLE_SUBMIT | ({'unknown'} if unknown_sent_nothing else set()) | (ATTEMPTED if retry_attempt else set())
                        for row in submit['applier_submits'])
                and {row['event_id'] for row in submit['applier_submit_auth']} == set(approvals))

    def requeue(self, job_id, previous_state, previous_folder, owner, batches, *, actor, reason, settled_auth=None, unknown_sent_nothing=False,
                retry_attempt=False):
        """Record that the user put the job back in the queue, and retire its Applier if it has one.

        The Applier's own rows move into the record, its questions close and undelivered messages to it are cancelled,
        so a new Applier for the job starts clean. A job with any Submit, skip or sync record is refused, unless the caller
        judged those records sent nothing and named every approval in settled_auth, used or not: then they move into the
        record too, which withdraws an unused one. unknown_sent_nothing is the user's statement that an attempt recorded
        as unknown sent nothing. Repeating the same requeue returns the first record."""
        _job(job_id), _text(actor, 'actor'), _text(reason, 'reason')
        entry_id = self.requeue_id(job_id, previous_state, previous_folder, owner)
        with self._write() as db:
            prior = db.execute('SELECT * FROM applier_requeues WHERE id=?', (entry_id,)).fetchone()
            if prior is not None:
                return dict(prior, batches=json.loads(prior['batches']), retired=json.loads(prior['retired']))
            submit = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE job_id=?', (job_id,))] for table in SUBMIT_TABLES}
            found = next((table for table, rows in submit.items() if rows), None)
            if found and settled_auth is None:
                raise ValueError(f'the job has a record in {found}, so it cannot go back to the queue')
            if found and not self._settled_unsent(submit, settled_auth, unknown_sent_nothing, retry_attempt):
                raise ValueError('its Submit records changed and no longer show that nothing was sent')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            retired = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE job_id=?', (job_id,))]
                       for table in RUN_TABLES if table in tables}
            if any(row['owner'] != owner for row in retired['applier_jobs']):
                raise ValueError('the Applier records belong to another owner')
            if found:
                retired |= {table: submit[table] for table in ('applier_submit_auth', 'applier_submits')}
            for table in retired:
                db.execute(f'DELETE FROM {table} WHERE job_id=?', (job_id,))
            live = ','.join('?' for _ in LIVE_REQUESTS)
            retired['applier_requests'] = [row['id'] for row in db.execute(f'SELECT id FROM applier_requests WHERE job_id=? AND state IN ({live})', (job_id, *LIVE_REQUESTS))]
            db.execute(f"UPDATE applier_requests SET state='retired' WHERE job_id=? AND state IN ({live})", (job_id, *LIVE_REQUESTS))
            retired['applier_messages'] = [row['id'] for row in db.execute("SELECT id FROM applier_messages WHERE job_id=? AND (delivered_at IS NULL OR outcome='claimed')", (job_id,))]
            db.execute("UPDATE applier_messages SET delivered_at=?,outcome='cancelled' WHERE job_id=? AND (delivered_at IS NULL OR outcome='claimed')", (_now(), job_id))
            entry = dict(id=entry_id, job_id=job_id, previous_state=previous_state, previous_folder=previous_folder, retired_owner=owner,
                         batches=list(batches), actor=actor, reason=reason, requeued_at=_now(), retired=retired)
            db.execute('INSERT INTO applier_requeues VALUES(:id,:job_id,:previous_state,:previous_folder,:retired_owner,:batches,:actor,:reason,:requeued_at,:retired)',
                       entry | {'batches': json.dumps(entry['batches']), 'retired': json.dumps(retired, ensure_ascii=False)})
            return entry

    def requeues(self, job_id):
        return [dict(row, batches=json.loads(row['batches']), retired=json.loads(row['retired']))
                for row in self._read('SELECT * FROM applier_requeues WHERE job_id=? ORDER BY requeued_at', (job_id,))]

    def skip(self, job_id):
        rows = self._read('SELECT reason,source_kind,source_id,sender_id,recorded_at FROM applier_skips WHERE job_id=?', (job_id,))
        return rows[0] if rows else None

    def unused_submit_auth(self, job_id):
        return bool(self._read('SELECT 1 FROM applier_submit_auth WHERE job_id=? AND used_at IS NULL', (job_id,)))

    def submit_state(self, job_id):
        rows = self._read('SELECT state,attempts,evidence,requested_at,outcome_at FROM applier_submits WHERE job_id=?', (job_id,))
        return rows[0] if rows else None

    def outbox(self, *, pending=False):
        return self._read('SELECT job_id,kind,state,attempts,last_error,created_at FROM applier_outbox' + (" WHERE state='pending'" if pending else '') + ' ORDER BY created_at', ())

    def outbox_result(self, job_id, error=None):
        with self._write() as db:
            if error is None:
                db.execute("UPDATE applier_outbox SET state='done',done_at=?,last_error=NULL,attempts=attempts+1 WHERE job_id=? AND state='pending'", (_now(), job_id))
            else:
                db.execute("UPDATE applier_outbox SET attempts=attempts+1,last_error=? WHERE job_id=? AND state='pending'", (str(error)[:500], job_id))

    def queue_message(self, job_id, text, message_id):
        """Queue a fixed program message for an Applier. The same message ID is queued once."""
        _job(job_id), _text(text, 'text'), _id(message_id, 'message_id')
        with self._write() as db:
            db.execute('INSERT OR IGNORE INTO applier_messages(id,job_id,text,created_at) VALUES(?,?,?,?)', (message_id, job_id, text, _now()))

    def claim_message(self, message_id):
        """Atomically take an undelivered message, so two processes never deliver it twice."""
        with self._write() as db:
            return db.execute("UPDATE applier_messages SET delivered_at=?,outcome='claimed' WHERE id=? AND delivered_at IS NULL", (_now(), message_id)).rowcount == 1

    def release_message(self, message_id):
        with self._write() as db:
            db.execute("UPDATE applier_messages SET delivered_at=NULL,outcome=NULL WHERE id=? AND outcome='claimed'", (message_id,))

    def cancel_message(self, message_id):
        with self._write() as db:
            db.execute("UPDATE applier_messages SET delivered_at=?,outcome='cancelled' WHERE id=? AND (delivered_at IS NULL OR outcome='claimed')", (_now(), message_id))

    def pending_messages(self):
        return self._read('SELECT id,job_id,text FROM applier_messages WHERE delivered_at IS NULL ORDER BY created_at', ())

    def claimed_messages(self):
        """Messages a delivery took and has not settled. A claim records its time in delivered_at."""
        return self._read("SELECT id,job_id,text,delivered_at AS claimed_at FROM applier_messages WHERE outcome='claimed' ORDER BY created_at", ())

    def message_delivered(self, message_id, outcome):
        with self._write() as db:
            db.execute("UPDATE applier_messages SET delivered_at=?,outcome=? WHERE id=? AND (delivered_at IS NULL OR outcome='claimed')", (_now(), outcome, message_id))

    def set_control(self, key, value):
        _id(key, 'key')
        with self._write() as db:
            if value is None:
                db.execute('DELETE FROM applier_control WHERE key=?', (key,))
            else:
                db.execute('INSERT INTO applier_control VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (key, str(value), _now()))

    def control(self, key):
        rows = self._read('SELECT value FROM applier_control WHERE key=?', (key,))
        return rows[0]['value'] if rows else None

    def auto_submit(self):
        """The user's auto submit mode: {enabled, enabled_at, enabled_by, source}. Off when the record is missing or unreadable."""
        off = dict(enabled=False, **dict.fromkeys(AUTO_SUBMIT_FIELDS))
        try:
            record = json.loads(self.control(AUTO_SUBMIT) or 'null')
        except (ValueError, sqlite3.Error):
            return off
        if not isinstance(record, dict) or record.get('enabled') is not True or not all(isinstance(record.get(k), str) and record[k] for k in AUTO_SUBMIT_FIELDS):
            return off
        return dict(enabled=True, **{k: record[k] for k in AUTO_SUBMIT_FIELDS})

    def set_auto_submit(self, enabled, *, actor, source):
        """Turn auto submit mode on or off. Only the user's own command calls this, never a model. Turning it on again keeps the first record."""
        _text(actor, 'actor'), _text(source, 'source')
        if not enabled:
            self.set_control(AUTO_SUBMIT, None)
        elif not self.auto_submit()['enabled']:
            self.set_control(AUTO_SUBMIT, json.dumps({'enabled': True, 'enabled_at': _now(), 'enabled_by': actor, 'source': source}))
        return self.auto_submit()

    def autopilot(self):
        """The user's standing authorization to scan, evaluate and apply: {enabled, changed_at, changed_by, source}.
        Off when the record is missing or unreadable."""
        try:
            record = json.loads(self.control(AUTOPILOT) or 'null')
        except (ValueError, sqlite3.Error):
            record = None
        if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k] for k in AUTOPILOT_FIELDS):
            return dict(enabled=False, **dict.fromkeys(AUTOPILOT_FIELDS))
        return dict(enabled=record.get('enabled') is True, **{k: record[k] for k in AUTOPILOT_FIELDS})

    def set_autopilot(self, enabled, *, actor, source):
        """Turn autopilot on or off and record who did it. Only the user's own command turns it on. Repeating the current
        mode keeps the record of who set it."""
        _text(actor, 'actor'), _text(source, 'source')
        current = self.autopilot()
        if current['enabled'] != bool(enabled) or current['changed_at'] is None:
            self.set_control(AUTOPILOT, json.dumps({'enabled': bool(enabled), 'changed_at': _now(), 'changed_by': actor, 'source': source}))
        return self.autopilot()

    def requests(self):
        return self._read('SELECT id,job_id,kind,blocker_type,field,state FROM applier_requests ORDER BY created_at', ())

    def captcha_reports(self):
        """Open captcha blockers, oldest first. The Applier ended its turn after each; the worker holds its job."""
        return self._read("SELECT id,job_id,text FROM applier_requests WHERE blocker_type='captcha' AND state='open' ORDER BY created_at", ())

    def unconfirmed_by_email(self):
        """Jobs whose submit_unknown blocker the confirmation mail check answered "not submitted" for want of an email."""
        rows = self._read("SELECT DISTINCT r.job_id FROM applier_replies p JOIN applier_requests r ON r.id=p.request_id "
                          "WHERE r.blocker_type='submit_unknown' AND p.source_kind='no_email'", ())
        return {row['job_id'] for row in rows}

    def ended_launches(self):
        """Launches recorded as having ended on their own: finished or failed, never a parked or deliberately stopped one."""
        return self._read("SELECT * FROM applier_launches WHERE state IN ('finished','failed')", ())

    def last_replies(self):
        """Each job's newest reply time, from the user or a program."""
        rows = self._read('SELECT r.job_id,MAX(p.received_at) AS at FROM applier_replies p JOIN applier_requests r ON r.id=p.request_id GROUP BY r.job_id', ())
        return {row['job_id']: row['at'] for row in rows}

    def browser_outages(self, before):
        """Open browser_disconnected blockers reported before the time before, oldest first."""
        return self._read("SELECT id,job_id FROM applier_requests WHERE blocker_type='browser_disconnected' AND state='open' AND created_at<? "
                          'ORDER BY created_at', (before,))

    def lost_after_submit(self):
        """Open tab_missing blockers of jobs whose Submit attempt has no seen outcome, oldest first."""
        attempted = ','.join('?' for _ in ATTEMPTED)
        return self._read("SELECT r.id,r.job_id,r.created_at FROM applier_requests r JOIN applier_submits s ON s.job_id=r.job_id "
                          f"WHERE r.blocker_type='tab_missing' AND r.state='open' AND s.state IN ({attempted}) ORDER BY r.created_at", tuple(ATTEMPTED))

    def lost_tabs(self):
        """Open tab_missing blockers of jobs with no Submit attempt that may have sent anything, oldest first. The Applier
        ended its turn after each; the worker puts its job back in the queue to be filled again."""
        retryable = ','.join('?' for _ in RETRYABLE_SUBMIT)
        return self._read("SELECT r.id,r.job_id FROM applier_requests r LEFT JOIN applier_submits s ON s.job_id=r.job_id "
                          f"WHERE r.blocker_type='tab_missing' AND r.state='open' AND (s.state IS NULL OR s.state IN ({retryable})) "
                          'ORDER BY r.created_at', tuple(RETRYABLE_SUBMIT))

    def requeued_by(self, actor):
        return self._read('SELECT id,job_id FROM applier_requeues WHERE actor=? ORDER BY requeued_at', (actor,))

    def captcha_solved(self, request_id, message):
        """Close a captcha blocker whose hold the user cleared, and queue the message that wakes its kept Applier, once."""
        with self._write() as db:
            row = db.execute("SELECT job_id FROM applier_requests WHERE id=? AND blocker_type='captcha' AND state='open'", (request_id,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE applier_requests SET state='cleared' WHERE id=?", (request_id,))
            db.execute('INSERT OR IGNORE INTO applier_messages(id,job_id,text,created_at) VALUES(?,?,?,?)', (f'captcha-{request_id}', row['job_id'], message, _now()))
            return True

    def record_resume(self, job_id, owner, resume_id, label, upload_name):
        """Remember which resume the Applier chose and the file name the employer receives."""
        _text(label, 'label'), _text(upload_name, 'upload_name')
        with self._write() as db:
            self._owned(db, job_id, owner)
            db.execute('INSERT OR REPLACE INTO applier_resumes VALUES(?,?,?,?,?)', (job_id, resume_id, label, upload_name, _now()))

    def resume(self, job_id):
        rows = self._read('SELECT resume_id,label,upload_name FROM applier_resumes WHERE job_id=?', (job_id,))
        return rows[0] if rows else None

    def filled_form(self, job_id):
        """Each field's latest read-back value, in the order the fields were first filled, and the newest readback ID."""
        rows = self._read('SELECT id,field,value FROM applier_readbacks WHERE job_id=? ORDER BY recorded_at,rowid', (job_id,))
        fields = {}
        for row in rows:
            fields[row['field']] = row['value']
        return list(fields.items()), rows[-1]['id'] if rows else None

    def readback_fields(self):
        return self._read('SELECT job_id,field FROM applier_readbacks', ())

    def alert(self, job_id, kind, detail):
        """Record one alert. The same job, kind and detail is recorded once."""
        _job(job_id), _id(kind, 'kind'), _text(detail, 'detail')
        with self._write() as db:
            return db.execute('INSERT OR IGNORE INTO applier_alerts VALUES(?,?,?,?,NULL)', (job_id, kind, detail, _now())).rowcount == 1

    def alert_sent(self, job_id, kind, detail):
        with self._write() as db:
            db.execute('UPDATE applier_alerts SET sent_at=? WHERE job_id=? AND kind=? AND detail=? AND sent_at IS NULL', (_now(), job_id, kind, detail))

    def reply_readbacks(self):
        """Readbacks of values that came from a user reply, oldest first."""
        return self._read('SELECT id,job_id,field,value,request_id,reply_version FROM applier_readbacks WHERE request_id IS NOT NULL ORDER BY recorded_at', ())

    def alerts(self, *, unsent=False):
        return self._read('SELECT job_id,kind,detail,created_at,sent_at FROM applier_alerts' + (' WHERE sent_at IS NULL' if unsent else '') + ' ORDER BY created_at', ())

    def reply(self, request_id, text, *, source_kind, source_id, sender_id, intent='answer'):
        """Save one human reply: an answer, or a change_request with feedback on the question's draft.
        Replaying the same source is a no-op; a new source adds a version."""
        _id(request_id, 'request_id'), _text(text, 'text'), _id(source_id, 'source_id'), _id(sender_id, 'sender_id')
        if source_kind not in REPLY_SOURCES:
            raise ValueError('unknown reply source')
        if intent not in REPLY_INTENTS:
            raise ValueError('unknown reply intent')
        with self._write() as db:
            current = db.execute('SELECT state,draft FROM applier_requests WHERE id=?', (request_id,)).fetchone()
            if current is None:
                raise ValueError('unknown request')
            if intent == 'change_request' and not current['draft']:
                raise ValueError('Only a question with a draft can get a change request.')
            if current['state'] in {'escalated', 'escalation_sent'}:
                raise ValueError('this blocker is being retried on a stronger model; wait for its new request')
            if current['state'] == 'withdrawn':
                raise ValueError('this job was skipped, so its questions are closed')
            if current['state'] == 'retired':
                raise ValueError('this job went back to the queue, so the questions of its old Applier are closed')
            prior = db.execute("SELECT request_id,version,text,COALESCE(intent,'answer') AS intent FROM applier_replies WHERE source_id=?", (source_id,)).fetchone()
            if prior is not None:
                if (prior['request_id'], prior['text'], prior['intent']) != (request_id, text, intent):
                    raise ValueError('a saved reply cannot be rewritten')
                return prior['version']
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM applier_replies WHERE request_id=?', (request_id,)).fetchone()[0]
            db.execute('INSERT INTO applier_replies(request_id,version,text,source_kind,source_id,sender_id,received_at,intent) VALUES(?,?,?,?,?,?,?,?)',
                       (request_id, version, text, source_kind, source_id, sender_id, _now(), intent))
            db.execute("UPDATE applier_requests SET state='answered' WHERE id=?", (request_id,))
            return version

    def _read(self, sql, params, *, migrated=False):
        if not self.path.exists():
            return []
        db = read_only(self.path)
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            # Another component may have created the file before this store wrote its schema.
            if 'no such table' in str(exc):
                return []
            # A store that no writer of this version has opened yet lacks the newest columns.
            if 'no such column' in str(exc) and not migrated:
                with self._write():
                    pass
                return self._read(sql, params, migrated=True)
            raise
        finally:
            db.close()

    def latest_reply(self, request_id):
        rows = self._read("SELECT version,text,source_kind,COALESCE(intent,'answer') AS intent FROM applier_replies WHERE request_id=? ORDER BY version DESC LIMIT 1", (request_id,))
        return rows[0] if rows else None

    def draft_feedback(self, limit=DRAFT_FEEDBACK_LIMIT):
        """The user's change requests on drafts across every job, newest first, so later drafts follow them."""
        if type(limit) is not int or limit <= 0:
            raise ValueError('limit must be a positive integer')
        return self._read("SELECT p.text AS feedback,r.text AS question,r.field,r.draft,r.company,r.job_id,p.received_at "
                          "FROM applier_replies p JOIN applier_requests r ON r.id=p.request_id WHERE p.intent='change_request' "
                          'ORDER BY p.received_at DESC,p.rowid DESC LIMIT ?', (limit,))

    def wait_for_reply(self, request_id, *, poll=1.0, stop=None):
        """Poll outside any transaction until a reply exists. Returns None once stop is set, or once the request is withdrawn
        because the job was skipped."""
        stop = stop or threading.Event()
        while (reply := self.latest_reply(request_id)) is None:
            if self.request(request_id)['state'] == 'withdrawn' or stop.wait(poll):
                return None
        return reply

    def note(self, job_id, owner, site, widget, text):
        _text(site, 'site'), _text(widget, 'widget'), _text(text, 'text')
        with self._write() as db:
            self._owned(db, job_id, owner)
            note_id = uuid.uuid4().hex
            db.execute('INSERT INTO applier_notes VALUES(?,?,?,?,?,?,?)', (note_id, site, widget, text, job_id, 'active', _now()))
            return note_id

    def notes(self, site):
        return self._read("SELECT widget,text FROM applier_notes WHERE site=? AND state='active' ORDER BY created_at", (_text(site, 'site'),))

    def register_tab(self, job_id, owner, tab_id, url):
        if type(tab_id) is not int or tab_id <= 0:
            raise ValueError('tab_id must be a positive integer')
        origin = _origin(url)
        with self._write() as db:
            self._owned(db, job_id, owner)
            missing = db.execute("SELECT tab_id FROM applier_tabs WHERE job_id=? AND state='missing'", (job_id,)).fetchall()
            decided = db.execute("SELECT 1 FROM applier_requests WHERE job_id=? AND blocker_type='tab_missing' AND state='answered'", (job_id,)).fetchone()
            if missing and str(tab_id) not in {r['tab_id'] for r in missing} and decided is None:
                raise ValueError('the original form tab is missing; report tab_missing and wait for the user before opening another tab')
            db.execute("INSERT INTO applier_tabs(job_id,tab_id,url,origin,recorded_at,state) VALUES(?,?,?,?,?,'observed') "
                       'ON CONFLICT(job_id,tab_id) DO UPDATE SET url=excluded.url,origin=excluded.origin,recorded_at=excluded.recorded_at',
                       (job_id, str(tab_id), url, origin, _now()))

    def close_tabs(self, job_id, owner):
        """Record that the Applier closed its tabs because its job no longer needs them: submitted, skipped, closed by the
        employer, or set aside for a CAPTCHA before any Submit, which a new Applier fills again in a fresh tab. Returns the tabs it marked."""
        with self._write() as db:
            self._owned(db, job_id, owner)
            submit = db.execute('SELECT state FROM applier_submits WHERE job_id=?', (job_id,)).fetchone()
            tried = submit is not None and submit['state'] not in RETRYABLE_SUBMIT
            finished = ((submit is not None and submit['state'] == 'submitted')
                        or db.execute('SELECT 1 FROM applier_skips WHERE job_id=?', (job_id,)).fetchone() is not None
                        or (not tried and db.execute("SELECT 1 FROM applier_requests WHERE job_id=? AND blocker_type='captcha' AND state='open'",
                                                     (job_id,)).fetchone() is not None))
            if not finished:
                raise ValueError('close your tabs only when a tool result tells you to: after record_submit_outcome with submitted, '
                                 'skip_job, report_closed, or a CAPTCHA before any Submit')
            rows = db.execute("SELECT tab_id,url FROM applier_tabs WHERE job_id=? AND state='observed'", (job_id,)).fetchall()
            db.execute("UPDATE applier_tabs SET state='closed' WHERE job_id=? AND state='observed'", (job_id,))
            return [dict(r) for r in rows]

    def tabs_closed(self, job_id):
        return bool(self._read("SELECT 1 FROM applier_tabs WHERE job_id=? AND state='closed'", (job_id,)))

    def mark_tabs_missing(self, job_id, owner):
        """Record that every live tab of the job is gone. Returns the tab IDs it marked."""
        with self._write() as db:
            self._owned(db, job_id, owner)
            rows = db.execute("SELECT tab_id,url FROM applier_tabs WHERE job_id=? AND state='observed'", (job_id,)).fetchall()
            db.execute("UPDATE applier_tabs SET state='missing' WHERE job_id=? AND state='observed'", (job_id,))
            return [dict(r) for r in rows]

    def progress(self, job_id, owner, step):
        _text(step, 'step')
        with self._write() as db:
            self._owned(db, job_id, owner)
            db.execute('UPDATE applier_jobs SET last_progress_at=?,last_progress=? WHERE job_id=?', (_now(), step, job_id))

    def readback(self, job_id, owner, field, value, *, request_id=None, reply_version=None):
        _text(field, 'field'), _text(value, 'value', empty=True)
        with self._write() as db:
            self._owned(db, job_id, owner)
            if request_id is not None:
                row = db.execute('SELECT MAX(r.version) AS v FROM applier_replies r JOIN applier_requests q ON q.id=r.request_id WHERE q.id=? AND q.job_id=?',
                                 (request_id, job_id)).fetchone()
                if row['v'] is None or row['v'] != reply_version:
                    raise ValueError('the reply version is stale; read the latest reply first')
            readback_id = uuid.uuid4().hex
            db.execute('INSERT INTO applier_readbacks VALUES(?,?,?,?,?,?,?)', (readback_id, job_id, field, value, request_id, reply_version, _now()))
            db.execute('UPDATE applier_jobs SET last_progress_at=?,last_progress=? WHERE job_id=?', (_now(), f'readback {field}', job_id))
            return readback_id
