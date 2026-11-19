"""Progress monitoring of Claude Appliers without a Codex database; synthetic fixtures, no CLI or worker starts."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.applier_runtime import ApplierRuntime
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.progress_monitor import LocalSources, ProgressMonitor
from test_claude_applier import FakeClaude


class ClaudeBatch:
    def __init__(self, base, jobs=(1,)):
        self.root = (Path(base) / 'runtime').resolve()
        self.manifest = self.root / '.pipeline/batches/claude.json'
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(dict(application_runtime='claude', target_successful_submissions=len(jobs), max_parallel=len(jobs),
                                                 ordered_candidates=[dict(id=j, company=f'Company {j}', title='Engineer') for j in jobs])))
        self.db = Path(base) / 'jobfeed.sqlite'
        with sqlite3.connect(self.db) as db:
            db.executescript('CREATE TABLE application_queue(job_id INTEGER, state TEXT, application_folder_relpath TEXT, codex_application_session_id TEXT);'
                             'CREATE TABLE jobs(id INTEGER, url TEXT, company TEXT); CREATE TABLE applied(job_id INTEGER);')
            db.executemany('INSERT INTO jobs(id,url) VALUES(?,?)', [(j, f'https://example.test/jobs/{j}') for j in jobs])
            db.executemany("INSERT INTO application_queue VALUES(?,'Queued',NULL,NULL)", [(j,) for j in jobs])
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        self.store = ApplierRuntime(self.root)
        self.sources = LocalSources(self.root, self.manifest, self.db)
        self.monitor = ProgressMonitor(self.sources, Path(base) / 'monitor-output/claude')

    def own(self, job_id, owner):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE application_queue SET state='Applying',codex_application_session_id=? WHERE job_id=?", (owner, job_id))

    def launch(self, job_id, owner):
        self.own(job_id, owner)
        self.launcher.launch(job_id, scope=str(self.manifest), url=f'https://example.test/jobs/{job_id}', materials=[],
                             work_dir=self.root / f'work-{job_id}', launch_id=owner)

    def job(self, job_id=1):
        snapshot = self.monitor.tick(force=True)
        if any(e.startswith('session_index') for e in snapshot['errors']):
            raise AssertionError(snapshot['errors'])
        return next(j for j in snapshot['jobs'] if j['job_id'] == job_id)


class ClaudeProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = ClaudeBatch(self.temp.name)
        self.owner = 'a' * 32

    def observed(self, job):
        return job['turn_state'], job['result_status'], job['blocked']

    def test_a_running_applier_is_active_and_its_progress_is_the_activity(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'contact section')
        job = self.b.job()
        self.assertEqual(self.observed(job), ('active', None, None))
        self.assertEqual(job['last_tool_at'], self.b.store.jobs()[0]['last_progress_at'])
        self.assertEqual(job['errors'], [])
        self.assertIsNone(job['chat_url'])
        self.assertEqual(job['next_step'], '等待原任务证据')

    def test_ready_for_review_ends_the_turn_with_that_result(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'ready for review')
        job = self.b.job()
        self.assertEqual(self.observed(job), ('ended', 'ready_for_review', None))
        self.assertEqual(job['errors'], [])
        self.assertTrue(job['reserved'])

    def test_a_question_or_user_check_waits_for_the_user(self):
        self.b.launch(1, self.owner)
        self.b.store.ask(1, self.owner, 'question', 'What is your notice period?', field='notice')
        self.assertEqual(self.observed(self.b.job()), ('ended', 'needs_input', 'needs_input'))
        verification = ClaudeBatch(Path(self.temp.name) / 'verification')
        verification.launch(1, self.owner)
        verification.store.ask(1, self.owner, 'blocker', 'Solve the captcha', blocker_type='user_only_check')
        self.assertEqual(self.observed(verification.job()), ('ended', 'needs_verification', 'needs_verification'))

    def test_an_escalated_component_blocker_is_a_retry_not_a_question(self):
        self.b.launch(1, self.owner)
        request = self.b.store.ask(1, self.owner, 'blocker', 'Date picker rejects input', blocker_type='broken_component', field='start')
        self.assertEqual(self.b.store.requests()[0]['state'], 'escalated', request)
        self.assertEqual(self.observed(self.b.job()), ('active', None, None))

    def test_a_submitted_step_without_a_receipt_is_a_claim_not_a_submission(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'submitted')
        job = self.b.job()
        self.assertEqual((job['turn_state'], job['result_status'], job['submitted']), ('ended', 'submitted', False))
        self.assertIn('submission_claim_requires_receipt', job['errors'])

    def test_a_waiting_request_wins_over_a_finished_step(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'ready for review')
        self.b.store.ask(1, self.owner, 'question', 'Confirm relocation?', field='relocation')
        self.assertEqual(self.observed(self.b.job()), ('ended', 'needs_input', 'needs_input'))

    def test_a_stopped_or_failed_unfinished_applier_is_interrupted(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'resume uploaded')
        for cli_state in ('stopped', 'failed'):
            self.b.fake.agents[0]['state'] = cli_state
            self.b.launcher.reconcile(self.owner)
            self.assertEqual(self.observed(self.b.job()), ('interrupted', None, None), cli_state)

    def test_a_parked_unfinished_applier_is_interrupted(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'resume uploaded')
        self.b.fake.agents[0]['state'] = 'blocked'
        self.b.launcher.park(self.owner)
        self.assertEqual(self.observed(self.b.job()), ('interrupted', None, None))

    def test_a_claude_owner_without_a_launch_record_is_unknown(self):
        self.b.own(1, self.owner)
        job = self.b.job()
        self.assertEqual(job['turn_state'], 'unknown')
        self.assertIn('claude_launch_missing', job['errors'])

    def test_a_queue_owner_that_is_not_the_launch_is_a_conflict(self):
        self.b.launch(1, self.owner)
        self.b.store.progress(1, self.owner, 'ready for review')
        self.b.own(1, 'b' * 32)
        job = self.b.job()
        self.assertEqual((job['turn_state'], job['result_status']), ('unknown', None))
        self.assertIn('owner_conflict', job['errors'])
        self.assertFalse(job['submitted'])

    def test_a_job_without_an_owner_waits_for_dispatch_without_any_source_error(self):
        snapshot = self.b.monitor.tick(force=True)
        self.assertEqual(snapshot['errors'], [])
        self.assertEqual(snapshot['jobs'][0]['turn_state'], 'not_started')


class ClaudeWatchArgumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'monitor-output/batch'

    def watch(self, runtime, *extra):
        from pipeline.cli import main
        manifest = self.root / f'{runtime}.json'
        manifest.write_text(json.dumps({'application_runtime': runtime}))
        argv = ['cli', 'watch', '--batch', str(manifest), '--progress-output', str(self.output), *extra]
        with patch('sys.argv', argv), patch('pipeline.cli.PipelineConfig.discover', return_value=SimpleNamespace(resume_root=self.root)), \
                patch('pipeline.worker_supervisor.supervise', return_value='no_actionable_work') as supervise, \
                patch('sys.stderr'), patch('sys.stdout'):
            try:
                return main(), supervise
            except SystemExit as exit:
                return exit.code, supervise

    def test_a_claude_batch_reports_locally_without_a_codex_database(self):
        code, supervise = self.watch('claude', '--progress-local-only')
        self.assertEqual(code, 0)
        self.assertEqual(supervise.call_args.args[-1][-1], '--worker-child')

    def test_a_claude_batch_without_a_codex_database_is_local_only(self):
        for extra in ((), ('--progress-local-only', '--progress-controller-thread', 'controller')):
            code, supervise = self.watch('claude', *extra)
            self.assertEqual(code, 2, extra)
            supervise.assert_not_called()


if __name__ == '__main__':
    unittest.main()
