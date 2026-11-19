from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from pipeline.answer_bank import AnswerBank
from pipeline.applier_runtime import ApplierRuntime
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.config import PipelineConfig
from pipeline.dashboard_submission import DashboardSubmissions
from test_claude_applier import FakeClaude
from tests.rerun_fixture import RerunFixture

OWNER = 'c' * 32


class ClaudeSubmissionCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = RerunFixture(Path(temporary.name) / 'runtime')
        self.config = replace(PipelineConfig.discover(self.fixture.root), jobfeed_db=self.fixture.db)
        with sqlite3.connect(self.fixture.db) as db:
            db.execute('UPDATE application_queue SET codex_application_session_id=? WHERE job_id=1', (OWNER,))
        self.fixture.gate.bind(str(self.fixture.scope), 1, OWNER)
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.fixture.root, run=self.fake.run, attach=self.fake.attach)
        self.launcher.launch(1, scope=str(self.fixture.scope), url='https://example.test/rilla', materials=[], work_dir=self.fixture.root / 'claude-work', launch_id=OWNER)
        for target, value in (('pipeline.claude_applier.ClaudeApplierLauncher', lambda root: self.launcher),):
            p = patch(target, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('pipeline.dashboard_submission.selected_materials', return_value={'job_id': 1, 'scope': str(self.fixture.scope), 'owner': OWNER, 'resume_id': 'original-pdf'})
        p.start()
        self.addCleanup(p.stop)
        for name in ('read_thread', 'send_existing'):
            p = patch('pipeline.dashboard_submission.' + name, side_effect=AssertionError(f'{name} must not run for a Claude owner'))
            p.start()
            self.addCleanup(p.stop)
        self.service = DashboardSubmissions(self.config)
        AnswerBank(self.fixture.root).pending('Why us?', 'The exact reviewed answer.', {'company': 'Rilla', 'job_id': '1'})


class ClaudeDashboardSubmissionTests(ClaudeSubmissionCase):
    def test_confirmation_authorizes_the_applier_and_wakes_it_without_codex(self):
        preview = self.service.preview(batch_id='test', job_id=1)
        self.assertTrue(preview['eligible'], preview.get('reason'))
        self.assertIsNone(preview['controller'])
        self.fake.agents[0].update(state='done', pid=None)
        request = str(uuid4())
        result = self.service.confirm(request_id=request, expected_preview=preview, confirmed=True)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(AnswerBank(self.fixture.root).read()['entries'][0]['versions'][-1]['state'], 'approved')
        allowed, reason = ApplierRuntime(self.fixture.root).request_submit(1, OWNER)
        self.assertEqual((allowed, reason), (True, 'authorized'))
        attach = [call for call in self.fake.calls if call[0] == 'attach']
        self.assertEqual(len(attach), 1)
        self.assertIn('request_submit', attach[0][2])
        again = self.service.confirm(request_id=request, expected_preview=preview, confirmed=True)
        self.assertEqual(again['state'], 'queued')
        self.assertEqual(len([c for c in self.fake.calls if c[0] == 'attach']), 1)

    def test_an_idle_applier_waiting_for_input_gets_the_message_typed(self):
        preview = self.service.preview(batch_id='test', job_id=1)
        self.fake.agents[0].update(state='blocked')
        self.service.confirm(request_id=str(uuid4()), expected_preview=preview, confirmed=True)
        attach = [c for c in self.fake.calls if c[0] == 'attach']
        self.assertEqual(len(attach), 1)
        self.assertIn('request_submit', attach[0][2])

    def test_a_failed_confirmation_leaves_no_usable_authorization_or_wake(self):
        preview = self.service.preview(batch_id='test', job_id=1)
        self.fake.agents[0].update(state='done', pid=None)
        self.launcher.attach = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('attach failed'))
        result = self.service.confirm(request_id=str(uuid4()), expected_preview=preview, confirmed=True)
        self.assertEqual(result['state'], 'blocked')
        runtime = ApplierRuntime(self.fixture.root)
        self.assertEqual(runtime.request_submit(1, OWNER), (False, 'not_authorized'))
        self.assertEqual(runtime.pending_messages(), [])

    def test_a_confirmation_interrupted_after_authorizing_leaves_no_usable_approval_or_wake(self):
        preview = self.service.preview(batch_id='test', job_id=1)
        self.fake.agents[0].update(state='done', pid=None)
        request = str(uuid4())
        with patch.object(ApplierRuntime, 'claim_message', side_effect=SystemExit('the Dashboard died here')), self.assertRaises(SystemExit):
            self.service.confirm(request_id=request, expected_preview=preview, confirmed=True)
        runtime = ApplierRuntime(self.fixture.root)
        self.assertTrue(runtime.unused_submit_auth(1))
        self.assertEqual(self.service.reconcile_pending()['results'][0]['state'], 'blocked')
        self.assertEqual(runtime.pending_messages(), [])
        self.assertEqual(runtime.request_submit(1, OWNER), (False, 'not_authorized'))
        self.assertEqual([c for c in self.fake.calls if c[0] == 'attach'], [])

    def test_a_form_the_applier_has_not_prepared_cannot_be_submitted_yet(self):
        with patch('pipeline.dashboard_submission.selected_materials', side_effect=FileNotFoundError('resume-source.json')):
            preview = self.service.preview(batch_id='test', job_id=1)
        self.assertFalse(preview['eligible'])
        self.assertIn('Submit is available once the row says Ready for review', preview['reason'])

    def test_a_running_applier_is_not_restarted(self):
        preview = self.service.preview(batch_id='test', job_id=1)
        self.service.confirm(request_id=str(uuid4()), expected_preview=preview, confirmed=True)
        self.assertEqual([c for c in self.fake.calls if c[0] == 'attach'], [])
        self.assertEqual(ApplierRuntime(self.fixture.root).request_submit(1, OWNER), (True, 'authorized'))

    def test_a_discord_submit_reply_runs_the_same_confirmation_once_and_records_who_sent_it(self):
        self.fake.agents[0].update(state='done', pid=None)
        confirm = dict(job_id=1, scope=str(self.fixture.scope), message_id='555', sender_id='400')
        with sqlite3.connect(self.fixture.root / '.pipeline/execution-gates.sqlite3') as db:
            db.execute('DELETE FROM execution_scope_cancellations')
            db.execute('DELETE FROM execution_cancellations')
        with self.assertRaisesRegex(ValueError, 'needs the Dashboard confirmation. Confirming approves the exact pending answer'):
            self.service.confirm_discord(**confirm)
        bank = AnswerBank(self.fixture.root)
        state = bank.read()
        bank.mutate({'action': 'approve', 'entry_id': state['entries'][0]['id'], 'expected_version': 1, 'expected_revision': state['revision']}, dashboard=True)
        self.assertEqual(self.service.confirm_discord(**confirm)['state'], 'queued')
        self.assertEqual(self.service.confirm_discord(**confirm)['state'], 'queued')
        attach = [c for c in self.fake.calls if c[0] == 'attach']
        self.assertEqual(len(attach), 1)
        self.assertIn('confirmed Submit in Discord', attach[0][2])
        with sqlite3.connect(self.fixture.db) as db:
            source = json.loads(db.execute('SELECT submit_authorization FROM application_queue WHERE job_id=1').fetchone()[0])['source']
        self.assertEqual((source['kind'], source['actor'], source['message_id']), ('discord', 'discord:400', '555'))
        runtime = ApplierRuntime(self.fixture.root)
        self.assertEqual(runtime.request_submit(1, OWNER), (True, 'authorized'))
        self.assertEqual(runtime.request_submit(1, OWNER), (False, 'already_requested'))

    def test_after_an_attempt_that_sent_nothing_one_new_confirmation_is_allowed(self):
        self.fake.agents[0].update(state='done', pid=None)
        preview = self.service.preview(batch_id='test', job_id=1)
        self.service.confirm(request_id=str(uuid4()), expected_preview=preview, confirmed=True)
        retry = self.service.preview(batch_id='test', job_id=1)
        with self.assertRaisesRegex(ValueError, 'must be reconciled'):
            self.service.confirm(request_id=str(uuid4()), expected_preview=retry, confirmed=True)
        runtime = ApplierRuntime(self.fixture.root)
        runtime.request_submit(1, OWNER)
        with self.assertRaisesRegex(ValueError, 'must be reconciled'):
            self.service.confirm(request_id=str(uuid4()), expected_preview=retry, confirmed=True)
        runtime.record_submit_outcome(1, OWNER, 'not_submitted', 'Missing entry for required field: LinkedIn Profile')
        self.assertEqual(self.service.confirm(request_id=str(uuid4()), expected_preview=self.service.preview(batch_id='test', job_id=1), confirmed=True)['state'], 'queued')
        self.assertEqual(runtime.request_submit(1, OWNER), (True, 'authorized'))
        with self.assertRaisesRegex(ValueError, 'must be reconciled'):
            self.service.confirm(request_id=str(uuid4()), expected_preview=self.service.preview(batch_id='test', job_id=1), confirmed=True)

    def earlier_request(self, state):
        with self.service.db() as db:
            db.execute("INSERT INTO requests(id,job,owner,state,preview,prompt,mode,created_at) VALUES(?,1,?,?,'{}','Submit now.','claude','2026-10-28')",
                       (str(uuid4()), OWNER, state))

    def test_an_earlier_request_recorded_as_not_submitted_does_not_block_a_new_confirmation(self):
        self.fake.agents[0].update(state='done', pid=None)
        self.earlier_request('not_submitted')
        result = self.service.confirm(request_id=str(uuid4()), expected_preview=self.service.preview(batch_id='test', job_id=1), confirmed=True)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(ApplierRuntime(self.fixture.root).request_submit(1, OWNER), (True, 'authorized'))

    def test_an_earlier_unreconciled_request_still_blocks_a_new_confirmation(self):
        for state in ('queued', 'running', 'blocked'):
            with self.subTest(state):
                with self.service.db() as db:
                    db.execute('DELETE FROM requests')
                self.earlier_request('not_submitted')
                self.earlier_request(state)
                with self.assertRaisesRegex(ValueError, 'must be reconciled'):
                    self.service.confirm(request_id=str(uuid4()), expected_preview=self.service.preview(batch_id='test', job_id=1), confirmed=True)
                self.assertEqual(ApplierRuntime(self.fixture.root).request_submit(1, OWNER), (False, 'not_authorized'))


class AutoSubmitTests(ClaudeSubmissionCase):
    """Auto submit mode runs the same confirmation as the Dashboard, for drafts that need nothing from the user."""

    def setUp(self):
        super().setUp()
        self.runtime = ApplierRuntime(self.fixture.root)
        self.fake.agents[0].update(state='done', pid=None)
        with sqlite3.connect(self.fixture.root / '.pipeline/execution-gates.sqlite3') as db:
            db.execute('DELETE FROM execution_scope_cancellations')
            db.execute('DELETE FROM execution_cancellations')
        bank = AnswerBank(self.fixture.root)
        state = bank.read()
        bank.mutate({'action': 'approve', 'entry_id': state['entries'][0]['id'], 'expected_version': 1, 'expected_revision': state['revision']}, dashboard=True)
        self.runtime.progress(1, OWNER, 'ready for review')

    def turn_on(self):
        return self.runtime.set_auto_submit(True, actor='user via test', source='test')

    def auto(self):
        return self.service.confirm_auto(job_id=1, scope=str(self.fixture.scope))

    def attaches(self):
        return [c for c in self.fake.calls if c[0] == 'attach']

    def test_auto_submit_is_off_by_default_and_a_ready_job_waits_for_the_user(self):
        self.assertFalse(self.runtime.auto_submit()['enabled'])
        self.assertIsNone(self.auto())
        self.assertEqual(self.runtime.request_submit(1, OWNER), (False, 'not_authorized'))
        self.assertEqual(self.attaches(), [])

    def test_a_ready_job_gets_exactly_one_confirmation_that_covers_one_attempt(self):
        mode = self.turn_on()
        first = self.auto()
        self.assertEqual(first['state'], 'queued')
        self.assertEqual(self.auto()['request_id'], first['request_id'])
        self.assertEqual(len(self.attaches()), 1)
        self.assertIn("auto submit mode confirmed Submit", self.attaches()[0][2])
        with sqlite3.connect(self.fixture.db) as db:
            source = json.loads(db.execute('SELECT submit_authorization FROM application_queue WHERE job_id=1').fetchone()[0])['source']
        self.assertEqual((source['kind'], source['actor'], source['enabled_by'], source['enabled_at']), ('auto', 'auto-submit', 'user via test', mode['enabled_at']))
        self.assertEqual(self.runtime.request_submit(1, OWNER), (True, 'authorized'))
        self.assertIsNone(self.auto())
        self.runtime.record_submit_outcome(1, OWNER, 'unknown', 'clicked once')
        self.assertIsNone(self.auto())
        self.assertEqual(self.runtime.request_submit(1, OWNER), (False, 'already_unknown'))
        self.runtime.record_submit_outcome(1, OWNER, 'submitted', 'Thanks for applying')
        self.runtime.progress(1, OWNER, 'ready for review')
        self.assertIsNone(self.auto())
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0], 1)

    def test_after_an_attempt_that_sent_nothing_the_fixed_draft_gets_one_new_confirmation(self):
        self.turn_on()
        first = self.auto()
        self.runtime.request_submit(1, OWNER)
        self.runtime.record_submit_outcome(1, OWNER, 'not_submitted', 'Missing entry for required field: LinkedIn Profile')
        self.assertIsNone(self.auto())
        self.runtime.progress(1, OWNER, 'ready for review')
        second = self.auto()
        self.assertEqual(second['state'], 'queued')
        self.assertNotEqual(second['request_id'], first['request_id'])
        self.assertEqual(self.auto()['request_id'], second['request_id'])
        self.assertEqual(self.runtime.request_submit(1, OWNER), (True, 'authorized'))
        self.assertEqual(self.runtime.submit_state(1)['attempts'], 2)

    def test_a_job_that_still_needs_the_user_is_left_for_the_user(self):
        self.turn_on()
        bank = AnswerBank(self.fixture.root)

        def ask(kind, text, **kwargs):
            request = self.runtime.ask(1, OWNER, kind, text, **kwargs)
            return lambda: self.runtime.reply(request, 'done', source_kind='dashboard', source_id=f'reply-{request}', sender_id='400')

        def pending_answer():
            bank.pending('Why this team?', 'A draft the user has not approved.', {'company': 'Rilla', 'job_id': '1'})
            return lambda: bank.mutate({'action': 'approve', 'entry_id': bank.read()['entries'][-1]['id'], 'expected_version': 1,
                                        'expected_revision': bank.read()['revision']}, dashboard=True)

        def draining():
            self.runtime.set_control('draining', '2026-11-04T00:00:00+00:00')
            return lambda: self.runtime.set_control('draining', None)

        def stopped():
            self.fake.agents[0].update(state='stopped')
            return lambda: self.fake.agents[0].update(state='done')

        def missing():
            agent = self.fake.agents.pop()
            return lambda: self.fake.agents.append(agent)

        conditions = {
            'open question': lambda: ask('question', 'What is your start date?', field='start_date'),
            'open CAPTCHA blocker': lambda: ask('blocker', 'Solve the hCaptcha.', blocker_type='user_only_check'),
            'answer pending review': pending_answer,
            'Appliers draining': draining,
            'Applier stopped': stopped,
            'Applier session missing': missing,
        }
        for reason, make in conditions.items():
            with self.subTest(reason):
                undo = make()
                self.assertIsNone(self.auto())
                self.assertEqual(self.runtime.request_submit(1, OWNER), (False, 'not_authorized'))
                undo()
        self.assertEqual(self.auto()['state'], 'queued')
        self.assertEqual(self.runtime.request_submit(1, OWNER), (True, 'authorized'))

    def test_a_parked_applier_at_review_is_confirmed_and_restarted(self):
        self.turn_on()
        self.fake.agents[0].update(state='done', pid=1)
        self.launcher.park(OWNER)
        self.assertEqual(self.auto()['state'], 'queued')
        self.assertIn("auto submit mode confirmed Submit", self.attaches()[0][2])
        self.assertEqual(self.launcher.reconcile(OWNER)['state'], 'running')
        self.assertEqual(self.runtime.request_submit(1, OWNER), (True, 'authorized'))

    def test_answers_the_applier_decided_show_approved_and_need_no_review(self):
        self.turn_on()
        from pipeline.applier_mcp import ApplierTools
        ApplierTools(self.runtime, 1, OWNER, company='Rilla', materials_root=self.fixture.root).record_answer(
            'Have you previously been employed by Rilla?', 'No', 'this_company')
        preview = self.service.preview(job_id=1, scope=str(self.fixture.scope))
        decided = [a for a in preview['answers'] if a['question'] == 'Have you previously been employed by Rilla?']
        self.assertEqual([(a['answer'], a['state']) for a in decided], [('No', 'approved')])
        self.assertEqual(preview['warnings'], [])
        self.assertEqual(self.auto()['state'], 'queued')
        self.assertEqual(self.runtime.request_submit(1, OWNER), (True, 'authorized'))

    def test_a_stopped_batch_or_a_held_job_is_left_for_the_user(self):
        self.turn_on()
        self.fixture.gate.cancel_scope(str(self.fixture.scope), 'user-stop')
        self.assertIsNone(self.auto())
        self.assertEqual(self.runtime.request_submit(1, OWNER), (False, 'not_authorized'))

    def test_turning_auto_submit_off_stops_further_confirmations(self):
        self.turn_on()
        off = self.runtime.set_auto_submit(False, actor='user via test', source='test')
        self.assertEqual(off, {'enabled': False, 'enabled_at': None, 'enabled_by': None, 'source': None})
        self.assertIsNone(self.auto())
        self.assertEqual(self.runtime.request_submit(1, OWNER), (False, 'not_authorized'))


class AutoSubmitModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = ApplierRuntime(self.root)

    def cli(self, mode):
        from types import SimpleNamespace
        from pipeline.cli import main
        with patch('sys.argv', ['cli', 'auto-submit', mode]), patch('pipeline.cli.PipelineConfig.discover', return_value=SimpleNamespace(resume_root=self.root)), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(), 0)
        return json.loads(out.getvalue())

    def test_the_cli_turns_the_mode_on_and_off_and_records_who_turned_it_on(self):
        self.assertFalse(self.cli('status')['enabled'])
        on = self.cli('on')
        self.assertEqual((on['enabled'], on['enabled_by'], on['source']), (True, 'user via pipeline.cli auto-submit', 'pipeline.cli auto-submit'))
        self.assertEqual(self.cli('on'), on)
        self.assertEqual(self.cli('status'), on)
        self.assertFalse(self.cli('off')['enabled'])
        self.assertFalse(self.runtime.auto_submit()['enabled'])

    def test_an_unreadable_record_means_off(self):
        for value in ('not json', '{"enabled": "yes", "enabled_at": "now", "enabled_by": "x", "source": "y"}', '{"enabled": true}'):
            with self.subTest(value):
                self.runtime.set_control('auto_submit', value)
                self.assertFalse(self.runtime.auto_submit()['enabled'])

if __name__ == '__main__':
    unittest.main()
