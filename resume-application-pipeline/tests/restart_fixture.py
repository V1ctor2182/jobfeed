from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import sqlite3

from pipeline.config import PipelineConfig
from pipeline.coordination import CoordinationStore
from pipeline.jobfeed import Jobfeed
from pipeline.resource_cli import task_contract
from pipeline.resume_catalog import ResumeCatalog, digest, encoded, prepare_application
from tests.rerun_fixture import RerunFixture


class RestartFixture(RerunFixture):
    def __init__(self, root):
        super().__init__(Path(root) / 'runtime')
        self.config = PipelineConfig.discover(self.root)
        self.config.jobfeed_db.parent.mkdir(parents=True)
        self.config.jobfeed_db.symlink_to(self.db)
        self.pdf = b'%PDF-1.4 synthetic frozen original'
        (self.root / 'original.pdf').write_bytes(self.pdf)
        (self.root / 'resume-catalog.json').write_text(json.dumps({'schema_version': 1, 'resumes': [
            {'id': 'original', 'label': 'Original PDF', 'available': True, 'pdf': 'original.pdf'}]}))
        catalog = ResumeCatalog.load(self.root / 'resume-catalog.json').freeze(self.root)
        self.manifest.update(resume_mode='selected_existing', resume_catalog=catalog, desktop_project_id='fixture-project')
        self.scope.write_text(json.dumps(self.manifest))
        self.job = Jobfeed(self.db, self.root.parent / 'jobfeed').get_job(1)
        self.folder = prepare_application(self.config, self.job, self.scope, catalog)
        facts = encoded({'schemaVersion': 1})
        (self.folder / 'selected.pdf').write_bytes(self.pdf)
        (self.folder / 'selected.autofill.json').write_bytes(facts)
        self.selection = {'schema_version': 2, 'kind': 'selected_existing', 'job_id': 1,
                          'scope': str(self.scope), 'owner': 'owner', 'resume_id': 'original',
                          'label': 'Original PDF', 'reason': 'Fixture confirmed existing resume',
                          'catalog_sha256': digest(encoded(catalog)), 'assets': {
                              'pdf': {'path': 'selected.pdf', 'sha256': digest(self.pdf)},
                              'facts': {'path': 'selected.autofill.json', 'sha256': digest(facts),
                                        'coverage': 'upload_only_no_resume_facts'}}}
        (self.folder / 'resume-source.json').write_bytes(encoded(self.selection))
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE application_queue SET application_folder_relpath=? WHERE job_id=1',
                       (str(self.folder.relative_to(self.root / 'applications')),))
        self.resources = CoordinationStore(self.root, scope=str(self.scope), jobfeed_db=self.db)
        self.resources.bind(1, 'owner')
        self.observe('observed')
        (self.folder / 'application-task.json').write_text(json.dumps({'job_id': 1, 'submit_authorized': True}))
        contract = task_contract(self.root, str(self.scope), 1, 'owner')
        contract['tab_associations'] = {'current': {'profile': 'fixture-profile', 'extension_instance': 'fixture-extension'}}
        (self.folder / 'resource-contract.json').write_text(json.dumps(contract))
        self.thread['createdAt'] = datetime.now(timezone.utc).timestamp() - 30
        self.threads = {'owner': self.thread}
        self.save_lifecycle()

    def observe(self, state):
        snapshot = self.resources.snapshot([1])
        return self.resources.observe_tabs(1, 'owner', [{'browser_id': 'saved-browser', 'tab_id': 'native-tab',
            'url': self.job.url, 'state': state, 'observation_id': 'inventory-' + str(snapshot['bindings'][0]['revision'])}], snapshot['bindings'][0]['revision'])

    def save_lifecycle(self):
        for owner, thread in getattr(self, 'threads', {'owner': self.thread}).items():
            events = [{'type': 'session_meta', 'payload': {'id': owner, 'cwd': str(self.root)}}]
            for turn in thread['turns']:
                start = turn.setdefault('fixture_started', (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())
                events.append({'type': 'event_msg', 'timestamp': start,
                               'payload': {'turn_id': turn['id'], 'type': 'task_started'}})
                state = self.host_states.get(turn['id'], turn['status'])
                if state != 'inProgress':
                    end = turn.setdefault('fixture_ended', datetime.now(timezone.utc).isoformat())
                    events.append({'type': 'event_msg', 'timestamp': end,
                                   'payload': {'turn_id': turn['id'], 'type': 'task_complete' if state == 'completed' else 'turn_aborted'}})
            Path(thread['path']).write_text(''.join(json.dumps(event) + '\n' for event in events))

    def read(self, owner):
        self.save_lifecycle()
        return self.threads[owner]

    def replacement(self, arguments, owner='replacement'):
        prompt = arguments['prompt']
        self.threads[owner] = {'id': owner, 'originator': 'Codex Desktop', 'cwd': str(self.root),
            'createdAt': datetime.now(timezone.utc).timestamp(), 'status': {'type': 'active'},
            'path': str(self.root / (owner + '-rollout.jsonl')), 'turns': [{
                'id': 'replacement-turn', 'status': 'inProgress', 'items': [{
                    'type': 'functionCallOutput', 'namespace': 'codex_app', 'name': 'create_thread',
                    'output': '<codex_delegation><source_thread_id>owner</source_thread_id><input>' + escape(prompt) + '</input></codex_delegation>'}]}]}
        self.save_lifecycle()
        return owner
