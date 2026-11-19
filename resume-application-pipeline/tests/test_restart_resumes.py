import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pipeline.config import PipelineConfig
from pipeline.lifecycle import ApplicationState
from pipeline.orchestrator import ApplicationPipeline
from pipeline import resume_catalog as resumes
from tests.runtime_fixture import RuntimeFixture


class RestartResumeTests(unittest.TestCase):
    request = '018fe000-1234-4567-89ab-0123456789ab'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = RuntimeFixture(temp.name)
        self.config = PipelineConfig.discover(self.fixture.root)
        self.scope = str(self.fixture.root)
        self.pipeline = ApplicationPipeline(self.config)
        self.pipeline.initialize()
        self.pipeline.enqueue(1)
        self.pipeline.process_completions()
        self.queue = self.pipeline.queue
        self.queue.transition(1, ApplicationState.APPLYING, codex_application_session_id='old-owner')
        self.gate = self.pipeline.applications.execution_gate
        self.gate.bind(self.scope, 1, 'old-owner')
        self.original = resumes.select_resume(self.config, self.scope, 1, 'old-owner',
                                              'fixture-original', 'Confirmed original PDF', binding_wait=0)
        self.queue.transition(1, ApplicationState.NEEDS_INPUT)
        self.folder = self.config.applications_root / self.queue.get(1).application_folder_relpath
        self.pointer = self.folder / 'resume-source.json'
        self.before = self.pointer.read_bytes()
        self.gate.bind(self.scope, 1, 'new-owner')
        self.gate.cancel_scope(self.scope, 'user-stop')
        self.transfer_permission = Mock(return_value=True)
        self.verify_permission = Mock(return_value=True)
        self.rollback_permission = Mock(return_value=True)
        module = SimpleNamespace(authorize_resume_transfer=self.transfer_permission,
                                 permits_resume_verify=self.verify_permission,
                                 authorize_resume_rollback=self.rollback_permission)
        self.stub = patch.dict('sys.modules', {'pipeline.application_restart': module})
        self.stub.start()
        self.addCleanup(self.stub.stop)

    def transfer(self):
        return resumes.transfer_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request)

    def adopt(self):
        self.transfer()
        self.queue.transition(1, ApplicationState.APPLYING, codex_application_session_id='new-owner')

    def verify(self, **kwargs):
        return resumes.verify_selection(self.config, self.scope, 1, 'new-owner',
                                         restart_request=self.request, turn_id='preparation-turn', **kwargs)

    def test_transfer_preserves_selection_and_assets_and_replays_after_owner_change(self):
        assets = {kind: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
                  for kind, path in self.original['paths'].items()}
        moved = self.transfer()
        self.assertEqual(moved, {**self.original, 'owner': 'new-owner'})
        self.assertEqual(json.loads((self.folder / 'resume-restarts' / (self.request + '.json')).read_text()),
                         {'request_id': self.request, 'previous_owner': 'old-owner', 'owner': 'new-owner',
                          'selection': json.loads(self.before)})
        self.queue.transition(1, ApplicationState.APPLYING, codex_application_session_id='new-owner')
        self.assertEqual(self.transfer(), moved)
        for kind, path in moved['paths'].items():
            self.assertEqual((Path(path).read_bytes(), Path(path).stat().st_mtime_ns), assets[kind])
        self.assertEqual(resumes.selected_materials(self.folder, owner='new-owner'), moved)
        with self.assertRaisesRegex(ValueError, 'owner mismatch'):
            resumes.selected_materials(self.folder, owner='old-owner')

    def test_transfer_requires_authoritative_grant_and_unchanged_stop(self):
        self.transfer_permission.return_value = False
        with self.assertRaisesRegex(ValueError, 'acknowledged'):
            self.transfer()
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.transfer_permission.side_effect = [True, False]
        with self.assertRaisesRegex(ValueError, 'no longer permits'):
            self.transfer()
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.assertFalse((self.folder / 'resume-restarts').exists())

    def test_transfer_rejects_tampered_pdf_and_foreign_owner(self):
        Path(self.original['paths']['pdf']).write_bytes(b'%PDF-1.4 changed')
        with self.assertRaisesRegex(ValueError, 'material changed'):
            self.transfer()
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.queue.transition(1, ApplicationState.APPLYING, codex_application_session_id='foreign-owner')
        with self.assertRaisesRegex(ValueError, 'queue owner mismatch'):
            self.transfer()

    def test_transfer_allows_pending_candidate_binding_but_rejects_foreign_binding(self):
        with self.gate._db() as db:
            db.execute("DELETE FROM execution_bindings WHERE owner='new-owner'")
        self.assertEqual(self.transfer(), {**self.original, 'owner': 'new-owner'})
        self.gate.bind(self.scope, 2, 'new-owner')
        with self.assertRaisesRegex(ValueError, 'execution binding mismatch'):
            self.transfer()

    def test_transfer_recovers_after_history_is_saved_without_reselecting(self):
        publish = resumes.publish
        def interrupted(path, data):
            if path == self.pointer:
                raise OSError('fixture interrupted before pointer publish')
            publish(path, data)
        with patch.object(resumes, 'publish', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.transfer()
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.assertEqual(self.transfer(), {**self.original, 'owner': 'new-owner'})

    def test_resume_verify_only_accepts_exact_live_preparation_permission(self):
        self.adopt()
        self.assertEqual(self.verify(), {**self.original, 'owner': 'new-owner'})
        self.verify_permission.assert_called_with(self.config, self.scope, 1, 'new-owner', self.request, 'preparation-turn')
        with self.assertRaises(ValueError):
            resumes.verify_selection(self.config, self.scope, 1, 'new-owner')
        with self.assertRaises(ValueError):
            resumes.select_resume(self.config, self.scope, 1, 'new-owner', 'fixture-original', 'same', binding_wait=0)
        self.verify_permission.return_value = False
        with self.assertRaisesRegex(ValueError, 'live restart'):
            self.verify()

    def test_verify_rechecks_permission_after_material_validation(self):
        self.adopt()
        self.verify_permission.side_effect = [True, False]
        with self.assertRaisesRegex(ValueError, 'no longer permits'):
            self.verify()

    def test_cli_requires_both_restart_identifiers_and_prints_exact_pdf(self):
        self.adopt()
        args = ['--root', str(self.fixture.root), 'verify', '--scope', self.scope,
                '--job', '1', '--owner', 'new-owner', '--restart-request', self.request]
        with self.assertRaisesRegex(ValueError, 'both request ID and turn ID'):
            resumes.main(args)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            resumes.main(args + ['--turn-id', 'preparation-turn'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['paths']['pdf'], self.original['paths']['pdf'])
        self.assertEqual(result['owner'], 'new-owner')

    def test_revoked_incomplete_adoption_restores_exact_original_receipt(self):
        self.transfer()
        self.assertEqual(resumes.restore_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request), self.original)
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.assertEqual(resumes.restore_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request), self.original)
        self.assertTrue((self.folder / 'resume-restarts' / (self.request + '.json')).is_file())

    def test_restore_requires_permission_and_cannot_revert_adopted_queue(self):
        self.transfer()
        self.rollback_permission.return_value = False
        with self.assertRaisesRegex(ValueError, 'revoked incomplete'):
            resumes.restore_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request)
        self.rollback_permission.return_value = True
        self.queue.transition(1, ApplicationState.APPLYING, codex_application_session_id='new-owner')
        with self.assertRaisesRegex(ValueError, 'previous queue owner'):
            resumes.restore_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request)
        self.assertEqual(json.loads(self.pointer.read_text())['owner'], 'new-owner')

    def test_restore_never_overwrites_changed_selection(self):
        self.transfer()
        changed = json.loads(self.pointer.read_text())
        changed['reason'] = 'unexpected replacement'
        self.pointer.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            resumes.restore_selection(self.config, self.scope, 1, 'old-owner', 'new-owner', self.request)
        self.assertEqual(json.loads(self.pointer.read_text()), changed)


if __name__ == '__main__':
    unittest.main()
