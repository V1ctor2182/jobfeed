from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from pipeline.coordination import CoordinationStore


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def contend(root, jobfeed_db, job_id, start, output, mode='claim'):
    try:
        store = CoordinationStore(root, scope='batch', jobfeed_db=jobfeed_db)
        start.wait(10)
        if mode == 'tabs':
            try:
                result = store.observe_tabs(job_id, f'owner-{job_id}', [{'browser_id': 'iab', 'tab_id': 'shared'}], 0)
                output.put('recorded')
            except ValueError as exc:
                if str(exc) != 'tab belongs to another job':
                    raise
                output.put('rejected')
        else:
            kind = ('account_mutation' if job_id == 1 else 'native_auth') if mode == 'account_aliases' else 'word'
            resource = store.snapshot([job_id])['accounts'][0]['resource_key'] if mode == 'account_aliases' else 'word-document-export'
            result = store.acquire(job_id, f'owner-{job_id}', kind, resource, f'request-{job_id}', f'secret-{job_id}')
            output.put(result['status'])
    except BaseException as exc:
        output.put(type(exc).__name__ + ': ' + str(exc))


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobfeed = self.root / 'jobfeed.sqlite3'
        with database(self.jobfeed) as db:
            db.execute('CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY,codex_application_session_id TEXT)')
            db.executemany('INSERT INTO application_queue VALUES(?,?)', [(1, 'owner-1'), (2, 'owner-2'), (3, 'owner-3')])
        self.store = CoordinationStore(self.root, scope='batch', jobfeed_db=self.jobfeed)

    def bind(self, job_id=1):
        return self.store.bind(job_id, f'owner-{job_id}')

    def claim(self, job_id=1, kind='word', resource='word-document-export', request=None, token=None):
        self.bind(job_id)
        return self.store.acquire(job_id, f'owner-{job_id}', kind, resource, request or f'request-{job_id}', token or f'secret-{job_id}')

    def evidence(self, observation='done'):
        return {'status': 'completed', 'observation_id': observation}

    def controller(self, active=True):
        (self.root / '.pipeline').mkdir(exist_ok=True)
        with database(self.root / '.pipeline' / 'blockers.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS dq_scopes(scope TEXT,controller TEXT,active INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS dq_jobs(scope TEXT,job TEXT,state TEXT,source TEXT)')
            db.execute("DELETE FROM dq_scopes WHERE scope='batch'")
            db.execute("DELETE FROM dq_jobs WHERE scope='batch'")
            db.execute('INSERT INTO dq_scopes VALUES(?,?,?)', ('batch', 'controller', int(active)))
            db.executemany("INSERT INTO dq_jobs VALUES('batch',?,'active','manifest')", [('1',), ('2',), ('3',)])

    def account(self, alias='applicant', jobs=None):
        if not (self.root / '.pipeline' / 'blockers.sqlite3').is_file():
            self.controller()
        return self.store.configure_account('controller', profile_id='profile', tenant_origin='https://tenant.invalid',
                                            account_alias=alias, job_ids=jobs or [1, 2, 3])['resource_key']

    def idle_evidence(self, claim, owner_ack=True):
        artifact = {'scope': 'batch', 'job_id': 1, 'owner': 'owner-1', 'claim_id': claim['claim_id'],
                    'observation_id': 'idle-1', 'resource_state': 'idle',
                    'observed_at': datetime.now(timezone.utc).isoformat()}
        if not owner_ack:
            artifact['prior_turn_id'] = 'stopped-turn'
        (self.root / 'idle.json').write_text(json.dumps(artifact))
        evidence = {'status': 'verified_idle', 'observation_id': 'idle-1', 'artifact_ref': 'idle.json'}
        if owner_ack:
            evidence['owner_ack_token'] = 'secret-1'
        return evidence

    def uncertain(self):
        claim = self.claim()
        return self.store.mark_uncertain(claim['claim_id'], 'secret-1', {'reason': 'transport_timeout'})

    def test_constructor_and_missing_snapshot_never_create_state(self):
        root = self.root / 'uncreated'
        store = CoordinationStore(root, scope='batch')
        self.assertEqual(store.snapshot(), {'scope': 'batch', 'bindings': [], 'tabs': [], 'claims': [], 'waits': [], 'accounts': []})
        self.assertFalse(root.exists())

    def test_current_owner_is_required_and_checked_again_on_mutation(self):
        with self.assertRaisesRegex(ValueError, 'owner unavailable'):
            CoordinationStore(self.root, scope='batch').bind(1, 'owner-1')
        with self.assertRaisesRegex(ValueError, 'owner mismatch'):
            self.store.bind(1, 'owner-2')
        self.claim()
        with database(self.jobfeed) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='replacement' WHERE job_id=1")
        with self.assertRaisesRegex(ValueError, 'owner mismatch'):
            self.store.observe_tabs(1, 'owner-1', [], 0)
        claim_id = self.store.snapshot()['claims'][0]['claim_id']
        with self.assertRaisesRegex(ValueError, 'owner mismatch'):
            self.store.release(claim_id, 'secret-1', self.evidence())

    def test_bind_replay_preserves_revision_and_rebinding_is_denied(self):
        self.assertEqual(self.bind()['revision'], 0)
        self.assertEqual(self.bind()['revision'], 0)
        with database(self.jobfeed) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='owner-1' WHERE job_id=2")
        with self.assertRaisesRegex(ValueError, 'another job'):
            self.store.bind(2, 'owner-1')

    def test_the_jobs_next_owner_takes_over_a_binding_its_retired_owner_left(self):
        binding = self.bind()
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'chrome', 'tab_id': '7', 'url': 'https://jobs.invalid/1'}], binding['revision'])
        with database(self.jobfeed) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='owner-next' WHERE job_id=1")
        rebound = self.store.bind(1, 'owner-next')
        self.assertEqual((rebound['owner'], rebound['revision']), ('owner-next', binding['revision'] + 2))
        self.assertEqual([(t['owner'], t['state']) for t in self.store.snapshot()['tabs']], [('owner-1', 'closed')])
        self.store.observe_tabs(1, 'owner-next', [{'browser_id': 'chrome', 'tab_id': '8', 'url': 'https://jobs.invalid/1'}], rebound['revision'])

    def test_a_retired_owners_unreleased_claim_keeps_the_binding(self):
        self.claim()
        with database(self.jobfeed) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='owner-next' WHERE job_id=1")
        with self.assertRaisesRegex(ValueError, 'already bound to another owner'):
            self.store.bind(1, 'owner-next')

    def test_acquisition_is_idempotent_and_never_exposes_token(self):
        first = self.claim()
        replay = self.claim()
        self.assertEqual(first, replay)
        snapshot = json.dumps(self.store.snapshot())
        self.assertNotIn('secret-1', snapshot)
        self.assertNotIn('token_hash', snapshot)
        self.assertEqual(len(self.store.snapshot()['claims']), 1)
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.claim(token='different')
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.claim(resource='different')

    def test_competing_claim_waits_durably_and_then_acquires(self):
        first = self.claim()
        wait = self.claim(2)
        self.assertEqual(wait['status'], 'waiting')
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['waits'][0]['status'], 'waiting')
        self.assertEqual(snapshot['waits'][0]['job_id'], 2)
        self.assertNotIn('token', json.dumps(snapshot['waits']))
        with self.assertRaisesRegex(ValueError, 'waiting request replay'):
            self.claim(2, resource='different')
        self.store.release(first['claim_id'], 'secret-1', self.evidence())
        second = self.claim(2)
        self.assertEqual(second['status'], 'held')
        self.assertEqual(self.store.snapshot()['waits'][0]['status'], 'acquired')
        self.assertEqual(self.claim()['status'], 'released')

    def test_independent_accounts_can_proceed(self):
        first, second = self.account('account-1'), self.account('account-2')
        self.assertEqual(self.claim(kind='account_mutation', resource=first)['status'], 'held')
        self.assertEqual(self.claim(2, kind='account_mutation', resource=second)['status'], 'held')

    def test_account_mutation_and_native_auth_share_conflict_namespace(self):
        key = self.account()
        first = self.claim(kind='account_mutation', resource=key)
        second = self.claim(2, kind='native_auth', resource=key)
        self.assertEqual(first['status'], 'held')
        self.assertEqual(second['status'], 'waiting')
        self.assertEqual(second['claim_id'], first['claim_id'])
        self.store.release(first['claim_id'], 'secret-1', self.evidence())
        self.assertEqual(self.claim(2, kind='native_auth', resource=key)['status'], 'held')

    def test_native_app_workflow_serializes_independent_account_flows(self):
        first, second = self.account('account-1'), self.account('account-2')
        self.assertEqual(self.claim(kind='account_mutation', resource=first)['status'], 'held')
        self.assertEqual(self.claim(2, kind='account_mutation', resource=second)['status'], 'held')
        first_app = self.claim(kind='native_workflow', resource='com.apple.AuthenticationServices', request='app-1')
        second_app = self.claim(2, kind='native_workflow', resource='com.apple.AuthenticationServices', request='app-2')
        self.assertEqual(first_app['status'], 'held')
        self.assertEqual(second_app['status'], 'waiting')
        independent_app = self.claim(3, kind='native_workflow', resource='com.microsoft.Word', request='app-3')
        self.assertEqual(independent_app['status'], 'held')
        self.store.release(first_app['claim_id'], 'secret-1', self.evidence('app-done'))
        self.assertEqual(self.claim(2, kind='native_workflow', resource='com.apple.AuthenticationServices', request='app-2')['status'], 'held')

    def cleanup_owner(self, *, job_id=1, owner='owner-1', scope='batch', remove_queue=True):
        if remove_queue:
            with database(self.jobfeed) as db:
                db.execute('DELETE FROM application_queue WHERE job_id=?', (job_id,))
        directory = self.root / '.pipeline' / 'submission-closeouts'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{job_id}.json').write_text(json.dumps({'job_id': job_id, 'folder': 'job-1', 'thread_id': owner, 'complete': False}))
        with database(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_bindings(owner TEXT PRIMARY KEY,scope TEXT,job_id INTEGER)')
            db.execute('INSERT OR REPLACE INTO execution_bindings VALUES(?,?,?)', (owner, scope, job_id))

    def test_original_cleanup_owner_can_record_closeout_and_release_existing_claim(self):
        claim = self.claim()
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'owned'}], 0)
        self.cleanup_owner()
        self.assertEqual(self.store.bind(1, 'owner-1')['revision'], 1)
        observed = self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'owned', 'state': 'closed', 'observation_id': 'cleanup-close'}], 1)
        self.assertEqual(observed['tabs'][0]['state'], 'closed')
        self.assertEqual(self.store.release(claim['claim_id'], 'secret-1', self.evidence())['status'], 'released')
        before = self.store.path.read_bytes()
        for kind in ('word', 'clipboard_foreground', 'account_mutation', 'native_auth', 'native_workflow'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'cleanup-only'):
                self.store.acquire(1, 'owner-1', kind, 'new-resource', 'cleanup-request', 'private-token')
        self.assertEqual(before, self.store.path.read_bytes())

    def test_original_cleanup_owner_can_reconcile_existing_uncertainty(self):
        claim = self.uncertain()
        self.controller()
        evidence = self.idle_evidence(claim)
        self.cleanup_owner()
        result = self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        self.assertEqual(result['status'], 'released')

    def test_closeout_record_requires_matching_execution_binding(self):
        self.cleanup_owner(scope='foreign-batch')
        with self.assertRaisesRegex(ValueError, 'owner unavailable for cleanup'):
            self.store.bind(1, 'owner-1')
        self.assertFalse(self.store.path.exists())
        self.cleanup_owner(owner='foreign-owner')
        with self.assertRaisesRegex(ValueError, 'owner unavailable for cleanup'):
            self.store.bind(1, 'owner-1')
        self.assertFalse(self.store.path.exists())

    def test_closeout_never_overrides_an_existing_different_queue_owner(self):
        self.cleanup_owner(remove_queue=False)
        with database(self.jobfeed) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='replacement' WHERE job_id=1")
        with self.assertRaisesRegex(ValueError, 'owner mismatch'):
            self.store.bind(1, 'owner-1')
        self.assertFalse(self.store.path.exists())

    def test_cleanup_without_durable_closeout_or_execution_binding_is_denied(self):
        with database(self.jobfeed) as db:
            db.execute('DELETE FROM application_queue WHERE job_id=1')
        with self.assertRaisesRegex(ValueError, 'owner unavailable for cleanup'):
            self.store.bind(1, 'owner-1')
        self.cleanup_owner()
        with database(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            db.execute('DELETE FROM execution_bindings')
        with self.assertRaisesRegex(ValueError, 'owner unavailable for cleanup'):
            self.store.bind(1, 'owner-1')
        self.assertFalse(self.store.path.exists())

    def test_account_registry_normalizes_origins_and_grants_exact_jobs(self):
        self.controller()
        first = self.store.configure_account('controller', profile_id='profile', tenant_origin='https://TENANT.invalid:443/', account_alias='primary', job_ids=[2, 1, 1])
        second = self.store.configure_account('controller', profile_id='profile', tenant_origin='https://tenant.invalid', account_alias='primary', job_ids=[3])
        self.assertEqual(first['resource_key'], second['resource_key'])
        self.assertEqual(len(first['resource_key']), 64)
        self.assertEqual(first['tenant_origin'], 'https://tenant.invalid')
        self.assertEqual(first['job_ids'], [1, 2])
        self.assertEqual([r['job_id'] for r in self.store.snapshot()['accounts']], [1, 2, 3])
        self.assertEqual([r['job_id'] for r in self.store.snapshot([2])['accounts']], [2])
        self.assertEqual(self.claim(resource=first['resource_key'], kind='account_mutation')['status'], 'held')
        self.assertEqual(self.claim(2, resource=second['resource_key'], kind='native_auth')['status'], 'waiting')

    def test_same_server_account_conflicts_across_distinct_browser_profiles(self):
        self.controller()
        first = self.store.configure_account('controller', profile_id='profile-a', tenant_origin='https://tenant.invalid', account_alias='same-applicant', job_ids=[1])
        second = self.store.configure_account('controller', profile_id='profile-b', tenant_origin='https://tenant.invalid', account_alias='same-applicant', job_ids=[2])
        self.assertEqual(first['resource_key'], second['resource_key'])
        self.assertNotEqual(first['account_ref'], second['account_ref'])
        accounts = self.store.snapshot()['accounts']
        self.assertEqual([(row['job_id'], row['profile_id']) for row in accounts], [(1, 'profile-a'), (2, 'profile-b')])
        self.assertEqual(self.claim(resource=first['resource_key'], kind='account_mutation')['status'], 'held')
        self.assertEqual(self.claim(2, resource=second['resource_key'], kind='native_auth')['status'], 'waiting')

    def test_unregistered_or_foreign_job_account_keys_refuse_without_mutation(self):
        self.bind()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'not registered'):
            self.store.acquire(1, 'owner-1', 'account_mutation', 'invented-alias', 'unregistered', 'token')
        self.assertEqual(before, self.store.path.read_bytes())
        key = self.account(jobs=[1])
        self.bind(2)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'not registered'):
            self.store.acquire(2, 'owner-2', 'native_auth', key, 'foreign-grant', 'token')
        self.assertEqual(before, self.store.path.read_bytes())
        clean_root = self.root / 'never-created'
        clean = CoordinationStore(clean_root, scope='batch', jobfeed_db=self.jobfeed)
        with self.assertRaisesRegex(ValueError, 'not registered'):
            clean.acquire(1, 'owner-1', 'account_mutation', key, 'unregistered-root', 'token')
        self.assertFalse(clean_root.exists())

    def test_account_registration_requires_active_controller_and_membership(self):
        self.controller()
        params = {'profile_id': 'profile', 'tenant_origin': 'https://tenant.invalid', 'account_alias': 'primary', 'job_ids': [1]}
        with self.assertRaisesRegex(ValueError, 'controller'):
            self.store.configure_account('owner-1', **params)
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(ValueError, 'member'):
            self.store.configure_account('controller', **{**params, 'job_ids': [99]})
        self.assertFalse(self.store.path.exists())
        self.controller(active=False)
        with self.assertRaisesRegex(ValueError, 'controller'):
            self.store.configure_account('controller', **params)
        self.assertFalse(self.store.path.exists())

    def test_account_registry_rejects_credentials_paths_and_contacts(self):
        self.controller()
        params = {'profile_id': 'profile', 'tenant_origin': 'https://tenant.invalid', 'account_alias': 'primary', 'job_ids': [1]}
        for origin in ('https://user:secret@tenant.invalid', 'https://tenant.invalid/path', 'https://tenant.invalid?otp=secret', 'https://tenant.invalid#token', ''):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.store.configure_account('controller', **{**params, 'tenant_origin': origin})
        with self.assertRaises(ValueError):
            self.store.configure_account('controller', **{**params, 'account_alias': 'person@example.com'})
        self.assertFalse(self.store.path.exists())

    def test_account_registry_uses_same_key_across_scopes_with_separate_grants(self):
        key = self.account(jobs=[1])
        with database(self.root / '.pipeline' / 'blockers.sqlite3') as db:
            db.execute("INSERT INTO dq_scopes VALUES('other','other-controller',1)")
            db.execute("INSERT INTO dq_jobs VALUES('other','2','active','manifest')")
        other = CoordinationStore(self.root, scope='other', jobfeed_db=self.jobfeed)
        registration = other.configure_account('other-controller', profile_id='profile', tenant_origin='https://tenant.invalid', account_alias='applicant', job_ids=[2])
        self.assertEqual(registration['resource_key'], key)
        self.assertEqual(self.store.snapshot()['accounts'][0]['job_id'], 1)
        self.assertEqual(other.snapshot()['accounts'][0]['job_id'], 2)
        self.assertEqual(self.claim(resource=key, kind='account_mutation')['status'], 'held')
        other.bind(2, 'owner-2')
        self.assertEqual(other.acquire(2, 'owner-2', 'native_auth', key, 'other-account', 'token')['status'], 'waiting')

    def test_reconstructed_store_does_not_expire_claim(self):
        claim = self.claim()
        with database(self.store.path) as db:
            db.execute("UPDATE coordination_claims SET updated_at='2000-01-01T00:00:00+00:00'")
        restarted = CoordinationStore(self.root, scope='batch', jobfeed_db=self.jobfeed)
        restarted.bind(2, 'owner-2')
        result = restarted.acquire(2, 'owner-2', 'word', 'word-document-export', 'restart', 'secret-2')
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['claim_id'], claim['claim_id'])

    def test_uncertainty_blocks_conflicting_work(self):
        claim = self.uncertain()
        self.assertEqual(claim['status'], 'needs_reconciliation')
        self.assertEqual(claim['revision'], 1)
        self.assertEqual(self.claim(2)['status'], 'waiting')
        with self.assertRaisesRegex(ValueError, 'token'):
            self.store.release(claim['claim_id'], 'wrong', self.evidence())
        with self.assertRaisesRegex(ValueError, 'controller reconciliation'):
            self.store.release(claim['claim_id'], 'secret-1', self.evidence())

    def test_completion_replay_is_exact(self):
        claim = self.claim()
        done = self.store.release(claim['claim_id'], 'secret-1', self.evidence())
        self.assertEqual(done['status'], 'released')
        self.assertEqual(done, self.store.release(claim['claim_id'], 'secret-1', self.evidence()))
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.store.release(claim['claim_id'], 'secret-1', self.evidence('changed'))
        with self.assertRaisesRegex(ValueError, 'cannot change'):
            self.store.mark_uncertain(claim['claim_id'], 'secret-1', {'reason': 'timeout'})

    def test_scope_isolation_and_global_resource_conflicts(self):
        self.claim()
        other = CoordinationStore(self.root, scope='other', jobfeed_db=self.jobfeed)
        self.assertEqual(other.snapshot()['claims'], [])
        other.bind(2, 'owner-2')
        self.assertEqual(other.acquire(2, 'owner-2', 'word', 'word-document-export', 'other', 'other-secret')['status'], 'waiting')
        with self.assertRaisesRegex(ValueError, 'this scope'):
            other.release(self.store.snapshot()['claims'][0]['claim_id'], 'secret-1', self.evidence())

    def test_tabs_persist_origins_popup_relationships_and_revision(self):
        self.bind()
        result = self.store.observe_tabs(1, 'owner-1', [
            {'browser_id': 'chrome', 'tab_id': 'parent', 'url': 'https://user:password@Example.com:443/jobs/123?token=secret#private'},
            {'browser_id': 'chrome', 'tab_id': 'child', 'opener_tab_id': 'parent', 'url': 'https://example.com/apply'}], 0)
        self.assertEqual(result['revision'], 1)
        parent, child = result['tabs']
        self.assertEqual(parent['origin'], 'https://example.com:443')
        self.assertEqual(child['opener_tab_id'], 'parent')
        self.assertNotIn('password', json.dumps(result))
        self.assertNotIn('token=secret', json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.store.observe_tabs(1, 'owner-1', [], 0)
        self.assertEqual(self.store.snapshot()['bindings'][0]['revision'], 1)

    def test_foreign_tab_claim_and_popup_are_rejected_atomically(self):
        self.bind()
        self.bind(2)
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        with self.assertRaisesRegex(ValueError, 'another job'):
            self.store.observe_tabs(2, 'owner-2', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        with self.assertRaisesRegex(ValueError, 'opener'):
            self.store.observe_tabs(2, 'owner-2', [{'browser_id': 'iab', 'tab_id': 'b', 'opener_tab_id': 'a'}], 0)
        self.assertEqual(self.store.snapshot([2])['tabs'], [])
        self.assertEqual(self.store.snapshot([2])['bindings'][0]['revision'], 0)

    def test_closed_tab_requires_observation_then_can_be_reused(self):
        self.bind()
        self.bind(2)
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        with self.assertRaises(ValueError):
            self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a', 'state': 'closed'}], 1)
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a', 'state': 'closed', 'observation_id': 'harness-close-1'}], 1)
        self.store.observe_tabs(2, 'owner-2', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        self.assertEqual(self.store.snapshot([1])['tabs'][0]['state'], 'closed')

    def test_missing_tab_remains_reserved_until_owner_confirms_closure(self):
        self.bind()
        self.bind(2)
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        missing = self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a', 'state': 'missing', 'observation_id': 'discovery-1'}], 1)
        self.assertEqual(missing['tabs'][0]['observation_id'], 'discovery-1')
        with self.assertRaisesRegex(ValueError, 'another job'):
            self.store.observe_tabs(2, 'owner-2', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)
        recovered = self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a', 'state': 'observed', 'observation_id': 'discovery-2'}], 2)
        self.assertEqual(recovered['tabs'][0]['state'], 'observed')
        self.assertEqual(len(recovered['tabs']), 1)
        self.store.observe_tabs(1, 'owner-1', [{'browser_id': 'iab', 'tab_id': 'a', 'state': 'closed', 'observation_id': 'closure-1'}], 3)
        self.assertEqual(self.store.observe_tabs(2, 'owner-2', [{'browser_id': 'iab', 'tab_id': 'a'}], 0)['tabs'][0]['owner'], 'owner-2')

    def test_malformed_identifiers_and_sensitive_evidence_are_rejected(self):
        self.bind()
        for resource in ('person@example.com', 'https://example.com', '../account', 'line\nbreak'):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.store.acquire(1, 'owner-1', 'account_mutation', resource, 'id', 'secret')
        claim = self.claim()
        with self.assertRaises(ValueError):
            self.store.release(claim['claim_id'], 'secret-1', {**self.evidence(), 'password': 'no'})
        with self.assertRaises(ValueError):
            self.store.release(claim['claim_id'], 'secret-1', {**self.evidence(), 'artifact_ref': '../outside'})
        for job in (True, 0, -1, '1'):
            with self.subTest(job=job), self.assertRaises(ValueError):
                self.store.bind(job, 'owner-1')

    def test_reconciliation_requires_active_controller_and_saved_idle_artifact(self):
        claim = self.uncertain()
        self.controller()
        evidence = self.idle_evidence(claim)
        with self.assertRaisesRegex(ValueError, 'controller'):
            self.store.reconcile(claim['claim_id'], 'foreign-controller', evidence, 1)
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.store.reconcile(claim['claim_id'], 'controller', evidence, 0)
        artifact = json.loads((self.root / 'idle.json').read_text())
        artifact['job_id'] = 2
        (self.root / 'idle.json').write_text(json.dumps(artifact))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        evidence = self.idle_evidence(claim)
        result = self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        self.assertEqual(result['status'], 'released')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.store.reconcile(claim['claim_id'], 'controller', evidence, 1), result)
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.store.reconcile(claim['claim_id'], 'controller', {**evidence, 'observation_id': 'different'}, 1)
        self.assertNotIn('secret-1', json.dumps(result))
        self.assertEqual(self.claim(2)['status'], 'held')

    def test_inactive_controller_cannot_reconcile(self):
        claim = self.uncertain()
        self.controller(active=False)
        with self.assertRaisesRegex(ValueError, 'active scope'):
            self.store.reconcile(claim['claim_id'], 'controller', self.idle_evidence(claim), 1)

    def test_idle_artifact_cannot_escape_workspace_through_symlink(self):
        claim = self.uncertain()
        self.controller()
        evidence = self.idle_evidence(claim)
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / 'idle.json'
            outside.write_text((self.root / 'idle.json').read_text())
            (self.root / 'idle.json').unlink()
            (self.root / 'idle.json').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'within workspace'):
                self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)

    def test_reconciliation_without_owner_ack_requires_verified_stop(self):
        claim = self.uncertain()
        self.controller()
        evidence = self.idle_evidence(claim, owner_ack=False)
        with self.assertRaisesRegex(ValueError, 'verified prior turn'):
            self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        with database(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            db.execute('CREATE TABLE execution_stops(owner TEXT,turn_id TEXT,status TEXT,observed_at TEXT)')
            db.execute("INSERT INTO execution_stops VALUES('owner-2','stopped-turn','completed',?)", (datetime.now(timezone.utc).isoformat(),))
        evidence['prior_turn_id'] = 'stopped-turn'
        with self.assertRaisesRegex(ValueError, 'unverified'):
            self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        with database(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            db.execute("INSERT INTO execution_stops VALUES('owner-1','stopped-turn','completed',?)", (datetime.now(timezone.utc).isoformat(),))
        evidence = self.idle_evidence(claim, owner_ack=False)
        evidence['prior_turn_id'] = 'stopped-turn'
        self.assertEqual(self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)['status'], 'released')

    def test_controller_can_reconcile_crashed_owner_held_claim(self):
        claim = self.claim()
        self.controller()
        evidence = self.idle_evidence(claim)
        done = self.store.reconcile(claim['claim_id'], 'controller', evidence, 0)
        self.assertEqual(done['status'], 'released')
        self.assertEqual(done['revision'], 1)
        self.assertEqual(self.store.reconcile(claim['claim_id'], 'controller', evidence, 0), done)

    def test_old_stop_cannot_release_a_new_claim(self):
        claim = self.uncertain()
        self.controller()
        evidence = self.idle_evidence(claim, owner_ack=False)
        evidence['prior_turn_id'] = 'stopped-turn'
        with database(self.root / '.pipeline' / 'execution-gates.sqlite3') as db:
            db.execute('CREATE TABLE execution_stops(owner TEXT,turn_id TEXT,status TEXT,observed_at TEXT)')
            db.execute("INSERT INTO execution_stops VALUES('owner-1','stopped-turn','completed','2000-01-01T00:00:00+00:00')")
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.reconcile(claim['claim_id'], 'controller', evidence, 1)
        self.assertEqual(self.store.snapshot()['claims'][0]['status'], 'needs_reconciliation')

    def test_snapshot_filters_jobs_without_mutating_files(self):
        self.claim()
        self.claim(2, resource='other-resource')
        before = self.store.path.read_bytes()
        self.assertEqual([r['job_id'] for r in self.store.snapshot([2])['claims']], [2])
        self.assertEqual(self.store.snapshot([])['claims'], [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_multiprocess_race_admits_only_one_resource_owner(self):
        self.bind()
        self.bind(2)
        context = multiprocessing.get_context('spawn')
        start, output = context.Event(), context.Queue()
        children = [context.Process(target=contend, args=(str(self.root), str(self.jobfeed), job, start, output)) for job in (1, 2)]
        for process in children:
            process.start()
        start.set()
        statuses = [output.get(timeout=15) for _ in children]
        for process in children:
            process.join(15)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail('coordination process did not exit')
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(statuses, ['held', 'waiting'])
        self.assertEqual(len(self.store.snapshot()['claims']), 1)
        self.assertEqual(len(self.store.snapshot()['waits']), 1)

    def test_multiprocess_tab_race_keeps_one_job_binding(self):
        self.bind()
        self.bind(2)
        context = multiprocessing.get_context('spawn')
        start, output = context.Event(), context.Queue()
        children = [context.Process(target=contend, args=(str(self.root), str(self.jobfeed), job, start, output, 'tabs')) for job in (1, 2)]
        for process in children:
            process.start()
        start.set()
        statuses = [output.get(timeout=15) for _ in children]
        for process in children:
            process.join(15)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail('tab coordination process did not exit')
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(statuses, ['recorded', 'rejected'])
        self.assertEqual(len(self.store.snapshot()['tabs']), 1)
        self.assertEqual(sum(r['revision'] for r in self.store.snapshot()['bindings']), 1)

    def test_multiprocess_account_alias_race_admits_one_account_owner(self):
        self.account()
        self.bind()
        self.bind(2)
        context = multiprocessing.get_context('spawn')
        start, output = context.Event(), context.Queue()
        children = [context.Process(target=contend, args=(str(self.root), str(self.jobfeed), job, start, output, 'account_aliases')) for job in (1, 2)]
        for process in children:
            process.start()
        start.set()
        statuses = [output.get(timeout=15) for _ in children]
        for process in children:
            process.join(15)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail('account coordination process did not exit')
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(statuses, ['held', 'waiting'])
        self.assertEqual(len(self.store.snapshot()['claims']), 1)
        self.assertEqual(len(self.store.snapshot()['waits']), 1)


if __name__ == '__main__':
    unittest.main()
