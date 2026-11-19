"""Synthetic stopped application runtime shared by command and HTTP tests."""
import json
from pathlib import Path
import sqlite3
from pipeline.cancellation import CancellationStore
from pipeline.decision_queue import DecisionQueue
from pipeline.store import ApplicationQueue

class RerunFixture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = self.root / '.pipeline/batches/test.json'
        self.scope.parent.mkdir(parents=True)
        self.manifest = {'controller_thread_id': 'controller', 'max_parallel': 1,
                         'ordered_candidates': [{'id': 1, 'company': 'Rilla', 'title': 'Engineer'}]}
        self.scope.write_text(json.dumps(self.manifest))
        self.scope.with_name('test-handoff.json').write_text(json.dumps({
            'manifest': str(self.scope), 'controller': 'controller', 'status': 'stopped_by_user'}))
        monitor = self.root / 'monitor-output/test'
        monitor.mkdir(parents=True)
        (monitor / 'monitor.json').write_text(json.dumps({'source': str(self.scope)}))
        self.folder = self.root / 'applications/rilla'
        self.folder.mkdir(parents=True)
        (self.folder / 'resume-source.json').write_text('{"resume_id":"original-pdf"}')
        self.db = self.root / 'jobs.sqlite3'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,company TEXT,title TEXT,jd_text TEXT,url TEXT,closed_at TEXT)')
            db.execute("INSERT INTO jobs VALUES(1,'Rilla','Engineer','Synthetic JD','https://example.test/application',NULL)")
            db.execute('CREATE TABLE applied(job_id INTEGER)')
        self.queue = ApplicationQueue(self.db)
        self.queue.initialize()
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO application_queue(job_id,state,application_folder_relpath,codex_application_session_id) VALUES(1,'Needs Input','rilla','owner')")
        ledger = DecisionQueue(self.root / '.pipeline/blockers.sqlite3', scope=str(self.scope), controller='controller')
        ledger.reconcile_membership([1], '2026-10-22T12:00:00+00:00')
        ledger.db.execute('UPDATE dq_scopes SET active=0')
        ledger.db.close()
        self.gate = CancellationStore(self.root / '.pipeline/execution-gates.sqlite3')
        self.gate.bind(str(self.scope), 1, 'owner')
        self.gate.bind(str(self.scope), 2, 'sibling')
        self.gate.cancel_scope(str(self.scope), 'original-stop')
        self.thread = {'id': 'owner', 'originator': 'Codex Desktop', 'cwd': str(self.root), 'status': {'type': 'idle'},
                       'turns': [{'id': 'old-turn', 'status': 'completed', 'items': []}]}
        self.sent = []
        self.host_states = {}
        self.thread['path'] = str(self.root / 'owner-rollout.jsonl')
        self.save_lifecycle()
    def save_lifecycle(self):
        events = [{'type':'session_meta', 'payload':{'id':'owner', 'cwd':str(self.root)}}]
        for turn in self.thread['turns']:
            state = self.host_states.get(turn['id'], turn['status'])
            event = {'inProgress':'task_started', 'completed':'task_complete', 'interrupted':'turn_aborted'}[state]
            events.append({'type':'event_msg', 'payload':{'turn_id':turn['id'], 'type':event}})
        Path(self.thread['path']).write_text(''.join(json.dumps(event)+'\n' for event in events))
    def read(self, owner):
        self.save_lifecycle()
        return self.thread
    def send(self, root, owner, prompt):
        self.sent.append((owner, prompt))
        return {'thread_id': owner, 'queued_message_id': 'queued-one', 'status': 'queued'}
    def running(self, prompt=None):
        self.thread['status'] = {'type': 'active'}
        self.thread['turns'].append({'id': 'new-turn', 'status': 'inProgress', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': prompt or self.sent[-1][1]}]}]})
        self.save_lifecycle()
