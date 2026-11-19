"""Durable, scope-bound decision queue. External sends occur only through send().

SQLite serializes dispatch admission with cancellation. A crash after admission is
unknown, never unsent. Transport callbacks must not re-enter this database.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
import threading


def stamp(value=None):
    parsed = value if isinstance(value, datetime) else (datetime.fromisoformat(value.replace('Z', '+00:00')) if value else datetime.now(timezone.utc))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def plus(value, seconds):
    return (datetime.fromisoformat(stamp(value)) + timedelta(seconds=seconds)).isoformat()


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class DecisionQueue:
    LEASE_SECONDS = 180
    WAIT_SECONDS = 180
    MAX_ATTEMPTS = 3

    def __init__(self, path, *, scope='legacy-review', controller='controller'):
        self.path, self.scope, self.controller = path, scope, controller
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._target = 'file:decision-' + uuid.uuid4().hex + '?mode=memory&cache=shared' if str(path)==':memory:' else str(path)
        self._uri = str(path)==':memory:'
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS dq_scopes(scope TEXT PRIMARY KEY, controller TEXT, active INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS dq_jobs(scope TEXT, job TEXT, state TEXT, source TEXT, PRIMARY KEY(scope,job));
        CREATE TABLE IF NOT EXISTS dq_issues(id INTEGER PRIMARY KEY, scope TEXT, job TEXT, identity TEXT,
          revision INTEGER, status TEXT, payload TEXT, attempts INTEGER DEFAULT 0, UNIQUE(scope,job,identity));
        CREATE TABLE IF NOT EXISTS dq_todos(id INTEGER PRIMARY KEY AUTOINCREMENT, issue INTEGER, scope TEXT,
          phase TEXT, status TEXT, revision INTEGER, token TEXT, holder TEXT, expires TEXT, created TEXT, summary TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS dq_one_live ON dq_todos(issue,phase) WHERE status IN ('queued','claimed');
        CREATE TABLE IF NOT EXISTS dq_actions(id TEXT PRIMARY KEY, issue INTEGER, scope TEXT, revision INTEGER,
          owner TEXT, state TEXT, payload TEXT, receipt TEXT, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS dq_executions(action TEXT PRIMARY KEY, owner TEXT, turn TEXT, evidence TEXT, observed TEXT);
        CREATE TABLE IF NOT EXISTS dq_requests(id TEXT PRIMARY KEY, issue INTEGER, scope TEXT, state TEXT, payload TEXT, receipt TEXT);
        CREATE TABLE IF NOT EXISTS dq_audit(id INTEGER PRIMARY KEY, scope TEXT, issue INTEGER, event TEXT, at TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS dq_audit_issue ON dq_audit(issue,id);
        CREATE TABLE IF NOT EXISTS dq_wakes(scope TEXT PRIMARY KEY, state TEXT, id TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS dq_meta(key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.execute('INSERT OR IGNORE INTO dq_scopes VALUES(?,?,1)', (scope, controller))
        self._migrate()

    @property
    def db(self):
        # Monitor polling and delivery run in different worker threads. Connections
        # are thread-owned; BEGIN IMMEDIATE serializes their write transactions.
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(self._target, timeout=10, isolation_level=None, uri=self._uri)
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')

    def _audit(self, event, issue=None, data=None, now=None):
        self.db.execute('INSERT INTO dq_audit(scope,issue,event,at,data) VALUES(?,?,?,?,?)',
                        (self.scope, issue, event, stamp(now), dump(data or {})))

    def _migrate(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issues'").fetchone():
            return
        with self.tx():
            if self.db.execute("SELECT 1 FROM dq_meta WHERE key='legacy_import'").fetchone():
                return
            for row in self.db.execute('SELECT * FROM issues').fetchall():
                p = dict(row)
                # Imported execution rights are quarantined, never inherited from history.
                status = 'closed' if p['status'] == 'closed' else 'diagnostic'
                self.db.execute('INSERT OR IGNORE INTO dq_issues(scope,job,identity,revision,status,payload) VALUES(?,?,?,?,?,?)',
                    ('migration-review', str(p['job_id']), 'legacy:'+str(p['id']), 1, status, dump(p)))
            self.db.execute("INSERT INTO dq_meta VALUES('legacy_import','complete; original tables retained; no dispatch authorization')")

    def bind(self, scope, controller=None):
        self.scope = scope
        if controller:
            self.controller = controller
        self.db.execute('INSERT OR IGNORE INTO dq_scopes VALUES(?,?,1)', (scope, self.controller))
        return self

    def _authorized(self, job=None, cleanup=False):
        scope = self.db.execute('SELECT * FROM dq_scopes WHERE scope=?', (self.scope,)).fetchone()
        if not scope or not scope['active'] or scope['controller'] != self.controller:
            raise ValueError('scope inactive or controller changed; recheck authorization')
        if job is not None:
            row = self.db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (self.scope,str(job))).fetchone()
            if job != 'system' and not row and self.db.execute('SELECT 1 FROM dq_meta WHERE key=?',('membership:'+self.scope,)).fetchone():
                raise ValueError('job outside current authorized membership')
            if row and row['state'] != 'active' and not (cleanup and row['state']=='completed'):
                raise ValueError('job cancelled/completed/paused; recheck scope')

    def reconcile_membership(self, jobs, now):
        """Only a successfully read manifest supplies this authoritative snapshot."""
        current = {str(job) for job in jobs}
        known = {r[0] for r in self.db.execute('SELECT job FROM dq_jobs WHERE scope=? UNION SELECT job FROM dq_issues WHERE scope=?', (self.scope,self.scope))}
        for job in known-current-{'system'}:
            self.cancel_job(job,'cancelled',{'reason':'removed_from_manifest'},now)
        with self.tx():
            for job in current:
                self.db.execute("INSERT OR IGNORE INTO dq_jobs VALUES(?,?,'active',?)",(self.scope,job,dump({'source':'manifest'})))
            self.db.execute('INSERT OR REPLACE INTO dq_meta VALUES(?,?)',('membership:'+self.scope,dump(sorted(current))))

    def _issue(self, issue_id):
        row = self.db.execute('SELECT * FROM dq_issues WHERE id=? AND scope=?', (issue_id,self.scope)).fetchone()
        if not row:
            raise ValueError('issue outside scope')
        p = json.loads(row['payload'])
        return {**p, **dict(row), 'payload':p, 'job_id':int(row['job']) if row['job'].isdigit() else None,
                'label':p.get('kind','review'), 'handler':'user' if row['status']=='parked' else 'controller',
                'thread':f"codex://threads/{p['owner']}" if p.get('owner') else None,
                'first_seen_at':p.get('first_seen_at'), 'last_seen_at':p.get('observed_at'),
                'done_at':p.get('done_at'), 'notified_at':p.get('notified_at'), 'episodes':1}

    def _enqueue(self, issue_id, phase, now):
        i = self._issue(issue_id)
        if phase in ('inspect_execution','reconcile','diagnose') and self.db.execute('SELECT 1 FROM dq_todos WHERE issue=? AND phase=? AND revision=? AND status="done"', (issue_id, phase, i['revision'])).fetchone():
            return
        self.db.execute("INSERT OR IGNORE INTO dq_todos(issue,scope,phase,status,revision,created) VALUES(?,?,?,'queued',?,?)",
                        (issue_id,self.scope,phase,i['revision'],stamp(now)))

    def observe_one(self, *, job, owner, features, evidence, now, complete=True, turn=None, kind='review', metadata=None):
        if not complete:
            self._audit('incomplete_observation', data={'job':job,'evidence':evidence}, now=now)
            return None
        # Features are semantic identifiers, not inferred from a category or screenshot.
        identity = key(features)
        with self.tx():
            self._authorized(job, cleanup=kind in ('sync','closeout'))
            row = self.db.execute('SELECT * FROM dq_issues WHERE scope=? AND job=? AND identity=?',
                                  (self.scope,str(job),identity)).fetchone()
            p = dict(metadata or {}, owner=owner, features=features, evidence=evidence, observed_at=stamp(now), turn=turn, kind=kind)
            if not row:
                p['first_seen_at'] = stamp(now)
                cur = self.db.execute("INSERT INTO dq_issues(scope,job,identity,revision,status,payload) VALUES(?,?,?,1,'review',?)",
                                      (self.scope,str(job),identity,dump(p)))
                issue_id = cur.lastrowid
                if p.get('defer_until'):
                    self.db.execute("UPDATE dq_issues SET status='program' WHERE id=?",(issue_id,))
                else:
                    self._enqueue(issue_id,'review',now)
            else:
                issue_id = row['id']; old=json.loads(row['payload'])
                if stamp(now) < old.get('observed_at', ''):
                    raise ValueError('stale observation')
                changed = old.get('evidence') != evidence or old.get('owner') != owner
                if old.get('defer_until'):p['defer_until']=old['defer_until']
                p = {**old,**p}
                if changed:
                    self.db.execute("UPDATE dq_actions SET state='cancelled' WHERE issue=? AND state='prepared'",(issue_id,))
                    self.db.execute('UPDATE dq_issues SET revision=revision+1,payload=? WHERE id=?',(dump(p),issue_id))
                    self.db.execute("UPDATE dq_todos SET status='superseded' WHERE issue=? AND status IN ('queued','claimed')",(issue_id,))
                    if row['status'] in ('review','closed'):
                        self.db.execute("UPDATE dq_issues SET status='review' WHERE id=?",(issue_id,))
                        self._enqueue(issue_id,'review',now)
                    elif row['status']=='waiting' and self.db.execute('SELECT 1 FROM dq_executions e JOIN dq_actions a ON a.id=e.action WHERE a.issue=?',(issue_id,)).fetchone():
                        self._enqueue(issue_id,'accept',now)
                    elif row['status']=='diagnostic':
                        self._enqueue(issue_id,'diagnose',now)
                    # Same semantic human request remains parked; evidence changes do not re-notify.
                else:
                    self.db.execute('UPDATE dq_issues SET payload=? WHERE id=?',(dump(p),issue_id))
            # An unchanged blocker seen again on a later tick adds nothing to its history (2026-11-17: 764,000 such rows).
            if not row or changed:
                self._audit('observation',issue_id,{'identity':identity},now)
        return self._issue(issue_id)

    def observe(self, jobs, errors, now):
        from pipeline.blocker_ledger import classify
        for j in jobs:
            if j.get('paused') or j.get('counted') is False:
                self.cancel_job(j['job_id'], 'cancelled' if j.get('queue_state') == 'Cancelled' else 'paused', {'source':'scope_observation'}, now)
                continue
            # A pause this observation set ends once the batch stops suspending the job; a pause the user set stays.
            with self.tx():
                self.db.execute("UPDATE dq_jobs SET state='active',source=? WHERE scope=? AND job=? AND state='paused' AND json_extract(source,'$.source')='scope_observation'",
                                (dump({'source':'scope_observation','resumed_at':now}),self.scope,str(j['job_id'])))
            if j.get('user_submitted') or j.get('submitted'):
                self.cancel_job(j['job_id'],'completed',{'source':'verified_submission_observation'},now)
            verdict = classify(j,now)
            if verdict:
                kind,_,evidence = verdict
                features = j.get('problem_features') or {'step':j.get('step','unknown'),
                    'target':j.get('field') or j.get('control_label') or kind,
                    'gap':j.get('semantic_gap') or j.get('result_quote') or evidence,
                    'applicability':j.get('company')}
                try:
                    self.observe_one(job=j['job_id'],owner=j.get('owner'),features=features,evidence=evidence,now=now,
                        complete=j.get('inspection_complete',True),turn=j.get('turn_id'),kind=kind,
                        metadata={'company':j.get('company'),'title':j.get('title'),'identity_review_required':not bool(j.get('problem_features')),
                            **({'defer_until':plus(now,900)} if kind in ('sync','closeout','terminal_closeout','needs_strong_model') else {})})
                except ValueError as exc:
                    self._audit('observation_rejected',data={'job':j['job_id'],'reason':str(exc)},now=now)
            # Absence and owner prose never prove resolution. Only deterministic terminal facts do.
            if j.get('synced') and j.get('submitted'):
                with self.tx():
                    for r in self.db.execute("SELECT id FROM dq_issues WHERE scope=? AND job=? AND status NOT IN ('closed','cancelled')",(self.scope,str(j['job_id']))).fetchall():
                        self._close(r['id'],{'source':'program_receipt_sync','job':j['job_id']},now)
        if errors:
            self.observe_one(job='system',owner=None,features={'operation':'source_read'},evidence=sorted(set(errors)),now=now,kind='system')
        self.recover(now)
        return []  # Human candidates are never emitted without structured controller review.

    def next(self, now, *, holder=None):
        self.recover(now)
        with self.tx():
            self._authorized()
            r = self.db.execute("SELECT * FROM dq_todos WHERE scope=? AND status='queued' ORDER BY id LIMIT 1",(self.scope,)).fetchone()
            if not r:return None
            token=uuid.uuid4().hex
            self.db.execute("UPDATE dq_todos SET status='claimed',holder=?,token=?,expires=? WHERE id=?",(holder or self.controller,token,plus(now,self.LEASE_SECONDS),r['id']))
        return self._todo(r['id'])

    def _todo(self, todo_id):
        r=dict(self.db.execute('SELECT * FROM dq_todos WHERE id=?',(todo_id,)).fetchone()); i=self._issue(r['issue'])
        return {**{k:v for k,v in i.items() if k not in ('id','status','payload')},**r,'issue_id':i['id'],
                'claim_token':r['token'],'issue_version':r['revision'],'attempt':i['attempts']+1,
                'instruction':('比较用户已接受的同类条款及授权范围；已覆盖就交原 Agent 继续，换公司本身不是重问理由。需询问时说明新增义务或范围差异。'
                               if i.get('kind') == 'binding_terms' else
                               '核对真实最新 turn、当前页、资料和授权；结构化决定，交付后立即领下一项'),
                'created_at':r['created'],'claimed_at':None,'history':self.history(i['id'])}

    HISTORY = 20

    def history(self, issue):
        """The issue's latest audit events, oldest first, without repeat observations.

        Every todo on the board carries this, and the worker rebuilds the board on each pass. With all of them it reached
        30 MB and 48 seconds a pass, and the worker ran out of memory (2026-11-17)."""
        rows = self.db.execute("SELECT at,event,data FROM dq_audit WHERE issue=? AND event!='observation' ORDER BY id DESC LIMIT ?",
                               (issue, self.HISTORY)).fetchall()
        return [dict(at=r['at'],summary=r['event']+': '+r['data'],needs_user=False) for r in reversed(rows)]

    def _claim(self,todo_id,token,revision,now):
        r=self.db.execute('SELECT * FROM dq_todos WHERE id=? AND scope=?',(todo_id,self.scope)).fetchone()
        if not r or r['status']!='claimed' or not token or r['token']!=token or r['holder']!=self.controller or r['expires']<=stamp(now):
            raise ValueError('stale/expired claim; recover and recheck')
        i=self._issue(r['issue']); self._authorized(i['job'],cleanup=i.get('kind') in ('sync','closeout'))
        if revision!=i['revision'] or revision!=r['revision']:raise ValueError('stale issue version; recheck')
        return r,i

    def prepare(self,todo_id,token,revision,action_id,owner,action,now):
        with self.tx():
            _,i=self._claim(todo_id,token,revision,now)
            if i.get('owner')!=owner:raise ValueError('original owner mismatch')
            job_state=self.db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?',(self.scope,i['job'])).fetchone()
            if job_state and job_state['state']=='completed' and action.get('operation') not in ('verify','sync','closeout'):
                raise ValueError('completed job permits only verification/sync/closeout')
            old=self.db.execute('SELECT * FROM dq_actions WHERE id=?',(action_id,)).fetchone()
            if old:
                if old['issue']!=i['id'] or json.loads(old['payload'])!=action:raise ValueError('action id reused')
                return dict(old)
            if action.get('operation') == 'verify':
                previous = self.db.execute('''SELECT a.payload FROM dq_actions a
                    JOIN dq_executions e ON e.action=a.id
                    WHERE a.issue=? AND a.revision=? AND a.owner=?''',
                    (i['id'], revision, owner)).fetchall()
                if any(json.loads(r['payload']).get('operation') == 'verify' for r in previous):
                    raise ValueError('verification evidence already exists for this issue/version; reuse it before requesting new evidence')
            if self.db.execute("SELECT 1 FROM dq_actions WHERE issue=? AND state IN ('prepared','sent','unknown')", (i['id'],)).fetchone():
                raise ValueError('unresolved action exists; reconcile or verify before preparing another action')
            if i['attempts']>=self.MAX_ATTEMPTS:raise ValueError('recovery budget exhausted; internal diagnosis required')
            self.db.execute('INSERT INTO dq_actions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (action_id,i['id'],self.scope,revision,owner,'prepared',dump(action),None,stamp(now),stamp(now)))
            self._audit('prepared',i['id'],{'action_id':action_id,'action':action},now)
        return dict(self.db.execute('SELECT * FROM dq_actions WHERE id=?',(action_id,)).fetchone())

    def send(self,todo_id,token,revision,action_id,transport,now):
        # Holding the transaction serializes cancellation admission against this send.
        # First persist unknown independently; never reset it following a transport error.
        with self.tx():
            _,i=self._claim(todo_id,token,revision,now)
            a=self.db.execute('SELECT * FROM dq_actions WHERE id=? AND issue=?',(action_id,i['id'])).fetchone()
            if not a or a['revision']!=revision:raise ValueError('action not prepared for current version')
            if a['state']!='prepared':return dict(a)
            self.db.execute("UPDATE dq_actions SET state='unknown',updated=? WHERE id=?",(stamp(now),action_id))
            self.db.execute('UPDATE dq_issues SET attempts=attempts+1 WHERE id=?',(i['id'],))
        with self.tx():
            self._claim(todo_id,token,revision,now)
            a=self.db.execute('SELECT * FROM dq_actions WHERE id=?',(action_id,)).fetchone()
            if a['state']!='unknown':return dict(a)
            try:
                receipt=transport(action_id,a['owner'],json.loads(a['payload']))
            except Exception as exc:
                self._audit('send_unknown',i['id'],{'error':type(exc).__name__},now)
                receipt=None
            if receipt:
                self.db.execute("UPDATE dq_actions SET state='sent',receipt=?,updated=? WHERE id=?",(dump(receipt),stamp(now),action_id))
            self.db.execute("UPDATE dq_todos SET status='done',summary='delivery admitted; awaiting evidence' WHERE id=?",(todo_id,))
            self.db.execute("UPDATE dq_issues SET status='waiting' WHERE id=?",(i['id'],))
        return dict(self.db.execute('SELECT * FROM dq_actions WHERE id=?',(action_id,)).fetchone())

    def decide(self,todo_id,token,revision,decision,basis,now,request=None):
        if not basis:raise ValueError('decision requires evidence basis')
        with self.tx():
            r,i=self._claim(todo_id,token,revision,now)
            states={'correct':'review','wait':'waiting','diagnose':'diagnostic','user':'parked','resolved':'closed'}
            if decision not in states:raise ValueError('invalid decision')
            if decision=='user':
                required={'action','evidence','checked_sources','why_unavailable','semantic_request','applicability'}
                if not request or not required.issubset(request) or any(not request[k] for k in required):raise ValueError('human request requires exact action, evidence, checked sources and scope')
                request_id=key([self.scope,i['job'],request['semantic_request'],request['applicability']])
                self.db.execute("INSERT OR IGNORE INTO dq_requests VALUES(?,?,?,'pending',?,NULL)",(request_id,i['id'],self.scope,dump(request)))
            updated = {**i['payload'], 'done_at':stamp(now)}
            self.db.execute('UPDATE dq_issues SET status=?,payload=? WHERE id=?',(states[decision],dump(updated),i['id']))
            self.db.execute("UPDATE dq_todos SET status='done',summary=? WHERE id=?",(dump(basis),todo_id))
            self._audit('decision:'+decision,i['id'],basis,now)
            if decision=='resolved':self._close(i['id'],basis,now)
        return self._issue(i['id'])

    def done(self,todo_id,summary,now,needs_user=False,*,token=None,revision=None,request=None):
        return self.decide(todo_id,token,revision,'user' if needs_user else 'wait',{'summary':summary},now,request)

    def report_execution(self,action_id,owner,turn,evidence,now):
        with self.tx():
            a=self.db.execute('SELECT * FROM dq_actions WHERE id=? AND scope=?',(action_id,self.scope)).fetchone()
            if not a or a['owner']!=owner:raise ValueError('execution does not match dispatched owner/action')
            if not turn or not evidence:raise ValueError('actual turn and evidence required')
            recorded = self.db.execute('SELECT owner,turn FROM dq_executions WHERE action=?', (action_id,)).fetchone()
            if recorded and (recorded['owner'] != owner or recorded['turn'] != turn):
                raise ValueError('execution conflicts with the recorded owner/turn')
            if a['state'] not in ('sent', 'unknown', 'verified', 'failed') or (not recorded and a['state'] in ('verified', 'failed')):
                raise ValueError('execution does not match dispatched owner/action')
            if recorded:
                return {'action_state': a['state'], 'already_recorded': True}
            self.db.execute('INSERT INTO dq_executions VALUES(?,?,?,?,?)',(action_id,owner,turn,dump(evidence),stamp(now)))
            i=self._issue(a['issue'])
            # Preserve late/in-flight execution facts without reviving cancelled
            # work or enqueueing acceptance of a superseded observation version.
            if i['status'] not in ('cancelled','closed'):
                self._enqueue(a['issue'],'accept',now)
            self._audit('execution_report',a['issue'],{'action':action_id,'turn':turn},now)
            return {'action_state': a['state'], 'already_recorded': False}

    def accept(self,action_id,revision,verifier,evidence,conclusion,now, revalidation=None):
        if verifier not in ('program',self.controller) or not evidence:raise ValueError('independent verifier and evidence required')
        with self.tx():
            a=self.db.execute('SELECT * FROM dq_actions WHERE id=? AND scope=?',(action_id,self.scope)).fetchone()
            if not a:raise ValueError('action outside scope')
            i=self._issue(a['issue'])
            self._authorized(i['job'],cleanup=i.get('kind') in ('sync','closeout'))
            if i['status']=='cancelled':raise ValueError('cancelled issue cannot be accepted')
            if verifier==a['owner'] or i['revision']!=revision:raise ValueError('owner cannot verify or stale result')
            if a['revision']!=revision and (not isinstance(revalidation,dict) or revalidation.get('action_revision')!=a['revision'] or revalidation.get('current_revision')!=revision or not revalidation.get('current_evidence') or not revalidation.get('basis')):
                raise ValueError('stale result requires independent current-evidence revalidation')
            if not self.db.execute('SELECT 1 FROM dq_executions WHERE action=?',(action_id,)).fetchone():raise ValueError('no execution evidence')
            if a['state'] in ('verified','failed'):
                expected='resolved' if a['state']=='verified' else 'failed'
                if conclusion!=expected:raise ValueError('conflicting terminal acceptance')
                return i
            if a['state'] not in ('sent','unknown'):raise ValueError('action is not awaiting verification')
            if conclusion in ('resolved','failed'):
                self.db.execute("UPDATE dq_todos SET status='done',summary='independent acceptance completed' WHERE issue=? AND phase='accept' AND status IN ('queued','claimed')",(i['id'],))
            if conclusion=='resolved':
                self._close(i['id'],evidence,now)
                self.db.execute("UPDATE dq_actions SET state='verified' WHERE id=?",(action_id,))
            elif conclusion=='failed':
                self.db.execute("UPDATE dq_actions SET state='failed' WHERE id=?",(action_id,))
                status='diagnostic' if i['attempts']>=self.MAX_ATTEMPTS else 'review'
                self.db.execute('UPDATE dq_issues SET status=? WHERE id=?',(status,i['id']))
                self._enqueue(i['id'],'diagnose' if status=='diagnostic' else 'review',now)
            elif conclusion!='waiting':raise ValueError('invalid acceptance')
            self._audit('accept:'+conclusion,i['id'],{'verifier':verifier,'evidence':evidence,'revalidation':revalidation},now)
        return self._issue(i['id'])

    def _close(self,issue,evidence,now):
        self.db.execute("UPDATE dq_issues SET status='closed' WHERE id=?",(issue,))
        self.db.execute("UPDATE dq_todos SET status='cancelled' WHERE issue=? AND status IN ('claimed','queued')",(issue,))
        self.db.execute("UPDATE dq_requests SET state='cancelled' WHERE issue=? AND state='pending'",(issue,))
        self._audit('verified_closed',issue,evidence,now)

    def cancel_job(self,job,state,source,now):
        if state not in ('paused','cancelled','completed'):raise ValueError('invalid cancellation')
        with self.tx():
            self.db.execute('INSERT INTO dq_jobs VALUES(?,?,?,?) ON CONFLICT(scope,job) DO UPDATE SET state=excluded.state,source=excluded.source',
                            (self.scope,str(job),state,dump(source)))
            rows=self.db.execute('SELECT id,payload FROM dq_issues WHERE scope=? AND job=?',(self.scope,str(job))).fetchall()
            for r in rows:
                if state=='completed' and json.loads(r['payload']).get('kind') in ('sync','closeout'):
                    continue
                self.db.execute("UPDATE dq_issues SET status='cancelled' WHERE id=? AND status!='closed'",(r['id'],))
                self.db.execute("UPDATE dq_todos SET status='cancelled' WHERE issue=? AND status IN ('claimed','queued')",(r['id'],))
                self.db.execute("UPDATE dq_actions SET state='cancelled' WHERE issue=? AND state='prepared'",(r['id'],))
                self.db.execute("UPDATE dq_requests SET state='cancelled' WHERE issue=? AND state='pending'",(r['id'],))
                self._audit('cancel:'+state,r['id'],source,now)

    def recover(self,now):
        with self.tx():
            for row in self.db.execute("SELECT * FROM dq_issues WHERE scope=? AND status='program'",(self.scope,)).fetchall():
                payload = json.loads(row['payload'])
                if payload.get('kind') in ('closeout', 'needs_strong_model'):
                    continue  # Ordinary cleanup stays with the program; delivery errors have their own issues.
                if payload.get('defer_until','9999')<=stamp(now):
                    self.db.execute("UPDATE dq_issues SET status='review' WHERE id=?",(row['id'],))
                    self._enqueue(row['id'],'review',now)
            for r in self.db.execute("SELECT * FROM dq_todos WHERE scope=? AND status='claimed' AND expires<=?",(self.scope,stamp(now))).fetchall():
                self.db.execute("UPDATE dq_todos SET status='expired' WHERE id=?",(r['id'],))
                self._enqueue(r['issue'],'recover',now)
                self._audit('lease_expired',r['issue'],{'prior_claim':r['id']},now)
            for row in self.db.execute("SELECT * FROM dq_issues WHERE scope=? AND status='waiting'", (self.scope,)).fetchall():
                p=json.loads(row['payload'])
                if p.get('done_at') and plus(p['done_at'],self.WAIT_SECONDS)<=stamp(now) and not self.db.execute('SELECT 1 FROM dq_actions WHERE issue=?',(row['id'],)).fetchone():
                    self.db.execute("UPDATE dq_issues SET status='diagnostic' WHERE id=?",(row['id'],))
                    self._enqueue(row['id'],'diagnose',now)
            for a in self.db.execute("SELECT * FROM dq_actions WHERE scope=? AND state IN ('sent','unknown') AND updated<=?",(self.scope,plus(now,-self.WAIT_SECONDS))).fetchall():
                i=self._issue(a['issue'])
                if i['status'] in ('closed','cancelled','diagnostic'):continue
                if a['state']=='unknown':
                    self.db.execute("UPDATE dq_issues SET status='diagnostic' WHERE id=?",(i['id'],))
                    self._enqueue(i['id'],'reconcile',now)
                elif not self.db.execute('SELECT 1 FROM dq_executions WHERE action=?',(a['id'],)).fetchone():
                    self._enqueue(i['id'],'inspect_execution',now)
                else:
                    self._enqueue(i['id'],'accept',now)

    def reconcile(self,action_id,receipt,now):
        if not receipt or not receipt.get('turn_id'):raise ValueError('positive owner turn evidence required; absence cannot prove unsent')
        with self.tx():
            a=self.db.execute('SELECT * FROM dq_actions WHERE id=? AND scope=?',(action_id,self.scope)).fetchone()
            if not a or a['state']!='unknown':raise ValueError('not an unknown action')
            self.db.execute("UPDATE dq_actions SET state='sent',receipt=?,updated=? WHERE id=?",(dump(receipt),stamp(now),action_id))
            i=self._issue(a['issue'])
            if i['status']=='diagnostic' and a['revision']==i['revision']:
                self.db.execute("UPDATE dq_issues SET status='waiting' WHERE id=?",(i['id'],))
                self.db.execute("UPDATE dq_todos SET status='done',summary='positive dispatch reconciliation' WHERE issue=? AND phase='reconcile' AND status IN ('queued','claimed')",(i['id'],))
            self._audit('reconciled',a['issue'],receipt,now)

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM dq_todos WHERE scope=? AND status='queued'",(self.scope,)).fetchone()[0]

    def parked(self):
        return [self._issue(r[0]) for r in self.db.execute("SELECT id FROM dq_issues WHERE scope=? AND status='parked'",(self.scope,))]

    def board(self):
        return {'requests':[dict(r) for r in self.db.execute('SELECT * FROM dq_requests WHERE scope=?',(self.scope,))],
                'diagnostic':[self._issue(r[0]) for r in self.db.execute("SELECT id FROM dq_issues WHERE scope=? AND status='diagnostic'",(self.scope,))],
                'parked':self.parked(),'queue':[self._todo(r[0]) for r in self.db.execute("SELECT id FROM dq_todos WHERE scope=? AND status IN ('queued','claimed') ORDER BY id",(self.scope,))],
                'waiting':[self._issue(r[0]) for r in self.db.execute("SELECT id FROM dq_issues WHERE scope=? AND status IN ('waiting','diagnostic')",(self.scope,))]}

    def wake_needed(self,last_wake_at,now):
        return bool(self.pending_count() and (not last_wake_at or stamp(now)>=plus(last_wake_at,120)))

    def deliver_requests(self,transport,now):
        """Persist unknown before sending; unknown deliveries require reconciliation."""
        results=[]
        for row in self.db.execute("SELECT * FROM dq_requests WHERE scope=? AND state='pending' ORDER BY rowid",(self.scope,)).fetchall():
            with self.tx():
                self._authorized()
                claimed=self.db.execute("UPDATE dq_requests SET state='unknown' WHERE id=? AND state='pending'",(row['id'],)).rowcount
            if not claimed:continue
            with self.tx():
                i=self._issue(row['issue']); self._authorized(i['job'])
                if i['status']!='parked':
                    self.db.execute("UPDATE dq_requests SET state='cancelled' WHERE id=?",(row['id'],));continue
                try:receipt=transport(row['id'],json.loads(row['payload']))
                except Exception:receipt=None
                if receipt:self.db.execute("UPDATE dq_requests SET state='sent',receipt=? WHERE id=?",(dump(receipt),row['id']))
                self._audit('user_request_delivery',i['id'],{'request':row['id'],'state':'sent' if receipt else 'unknown'},now)
                results.append({'id':row['id'],'state':'sent' if receipt else 'unknown'})
        return results

    def user_answer(self,job,source,now):
        # Caller is the controller intake; an owner report cannot supply this.
        if not source.get('verified_human') or not source.get('message_id'):raise ValueError('verified human source required')
        touched=[]
        with self.tx():
            for r in self.db.execute("SELECT * FROM dq_issues WHERE scope=? AND job=? AND status IN ('parked','review','waiting','diagnostic')",(self.scope,str(job))).fetchall():
                p=json.loads(r['payload'])
                if p.get('user_source',{}).get('message_id')==source['message_id']:
                    touched.append(r['id']);continue
                p['user_source']=source
                self.db.execute("UPDATE dq_actions SET state='cancelled' WHERE issue=? AND state='prepared'",(r['id'],))
                self.db.execute("UPDATE dq_requests SET state='cancelled' WHERE issue=? AND state='pending'",(r['id'],))
                self.db.execute("UPDATE dq_todos SET status='superseded' WHERE issue=? AND status IN ('queued','claimed')",(r['id'],))
                action=self.db.execute("SELECT * FROM dq_actions WHERE issue=? AND state IN ('sent','unknown')",(r['id'],)).fetchone()
                phase='review';status='review'
                if action:
                    status='waiting'
                    phase='accept' if self.db.execute('SELECT 1 FROM dq_executions WHERE action=?',(action['id'],)).fetchone() else 'inspect_execution'
                self.db.execute("UPDATE dq_issues SET status=?,revision=revision+1,payload=? WHERE id=?",(status,dump(p),r['id']))
                self._enqueue(r['id'],phase,now)
                self._audit('user_answer',r['id'],source,now)
                touched.append(r['id'])
        return touched

    def heartbeat_claim(self,now):
        """Only the program heartbeat calls this; unknown wake is never resent."""
        with self.tx():
            self._authorized()
            if not self.pending_count():return None
            r=self.db.execute('SELECT * FROM dq_wakes WHERE scope=?',(self.scope,)).fetchone()
            if r and r['state'] in ('unknown','sent'):return None
            if r and r['state']=='prepared':return r['id']
            wake_id=uuid.uuid4().hex
            self.db.execute("INSERT INTO dq_wakes VALUES(?,'prepared',?,?) ON CONFLICT(scope) DO UPDATE SET state='prepared',id=excluded.id,updated=excluded.updated",(self.scope,wake_id,stamp(now)))
            return wake_id

    def heartbeat_ack(self,wake_id,now):
        with self.tx():
            self._authorized()
            if not self.db.execute("UPDATE dq_wakes SET state='sent',updated=? WHERE scope=? AND id=? AND state IN ('prepared','unknown')",(stamp(now),self.scope,wake_id)).rowcount:
                raise ValueError('stale heartbeat receipt')

    def controller_idle(self,now):
        with self.tx():
            self._authorized()
            if self.db.execute("SELECT 1 FROM dq_todos WHERE scope=? AND status='claimed'",(self.scope,)).fetchone():
                raise ValueError('controller still owns a claim')
            self.db.execute("UPDATE dq_wakes SET state='idle',updated=? WHERE scope=?",(stamp(now),self.scope))
            # A new enqueue racing with idle remains visible to next heartbeat.
            return self.pending_count()

    def heartbeat_terminal(self,wake_id,receipt,now):
        if not receipt or not receipt.get('turn_id') or receipt.get('status') not in ('completed','interrupted','failed'):
            raise ValueError('positive matching controller terminal evidence required')
        with self.tx():
            self._authorized()
            changed=self.db.execute("UPDATE dq_wakes SET state='idle',updated=? WHERE scope=? AND id=? AND state='sent'",(stamp(now),self.scope,wake_id)).rowcount
            if changed:self._audit('wake_terminal',data={'wake_id':wake_id,'receipt':receipt},now=now)
            return bool(changed)

    def heartbeat_start(self,wake_id,now):
        with self.tx():
            self._authorized()
            if not self.db.execute("UPDATE dq_wakes SET state='unknown',updated=? WHERE scope=? AND id=? AND state='prepared'",(stamp(now),self.scope,wake_id)).rowcount:
                raise ValueError('wake already sent/unknown; reconcile only')

    def identify(self,todo_id,token,revision,target_issue,now,basis):
        """Controller resolves ambiguous wording; preserves evidence and restrictive state."""
        if not basis:raise ValueError('identity decision requires concrete evidence')
        with self.tx():
            r,i=self._claim(todo_id,token,revision,now);target=self._issue(target_issue)
            if target['job']!=i['job'] or target['id']==i['id']:raise ValueError('identity merge must stay within job and scope')
            if target['status'] in ('cancelled','closed'):raise ValueError('re-review terminal target before linking')
            self.db.execute("UPDATE dq_issues SET status='linked' WHERE id=?",(i['id'],))
            self.db.execute("UPDATE dq_todos SET status='cancelled' WHERE issue=? AND status IN ('queued','claimed')",(i['id'],))
            self._audit('identity_link',i['id'],{'target':target_issue,'basis':basis},now)
        return self._issue(target_issue)

    def metrics(self):
        return {
            'human_requests':self.db.execute('SELECT count(*) FROM dq_requests WHERE scope=?',(self.scope,)).fetchone()[0],
            'unknown_requests':self.db.execute("SELECT count(*) FROM dq_requests WHERE scope=? AND state='unknown'",(self.scope,)).fetchone()[0],
            'actions':self.db.execute('SELECT count(*) FROM dq_actions WHERE scope=?',(self.scope,)).fetchone()[0],
            'issues':[{'issue_id':r['id'],'status':r['status'],'first_seen_at':json.loads(r['payload']).get('first_seen_at'),'last_observed_at':json.loads(r['payload']).get('observed_at')} for r in self.db.execute('SELECT * FROM dq_issues WHERE scope=?',(self.scope,))],
        }

    def authorize_recovery(self,todo_id,token,revision,method,evidence,now):
        """A diagnostic can authorize a new bounded budget only after new observation."""
        if not method or not evidence:raise ValueError('new method and observed evidence required')
        with self.tx():
            r,i=self._claim(todo_id,token,revision,now)
            prior=self.db.execute('SELECT max(revision) FROM dq_actions WHERE issue=?',(i['id'],)).fetchone()[0]
            if i['status']!='diagnostic' or prior is None or revision<=prior or evidence!=i['evidence']:
                raise ValueError('diagnostic budget needs new observed evidence, not renamed action')
            if self.db.execute("SELECT 1 FROM dq_actions WHERE issue=? AND state IN ('prepared','sent','unknown')",(i['id'],)).fetchone():
                raise ValueError('unresolved delivery must be reconciled before new recovery')
            self.db.execute("UPDATE dq_issues SET attempts=0,status='review' WHERE id=?",(i['id'],))
            self.db.execute("UPDATE dq_todos SET status='done' WHERE id=?",(todo_id,))
            self._audit('new_recovery_budget',i['id'],{'method':method,'evidence':evidence},now)
            self._enqueue(i['id'],'review',now)
