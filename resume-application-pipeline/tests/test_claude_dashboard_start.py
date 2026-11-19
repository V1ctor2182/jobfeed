"""Dashboard 'Start with Claude' requests; synthetic fixtures, no real worker or Claude session starts."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_claude_applier import FakeClaude
from runtime_fixture import RuntimeFixture
from pipeline.applier_runtime import ApplierRuntime
from pipeline.batch import BatchPipeline
from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.local_state import now
from pipeline.claude_dashboard_start import Host
from pipeline.config import PipelineConfig
from pipeline.dashboard_queue import DashboardQueue


class ClaudeDashboardStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = RuntimeFixture(self.temp.name)
        self.root = self.f.root
        (self.root / 'application-profile.md').write_text('Synthetic confirmed profile.\n')
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        for name in ('claude', 'jobfeed'):
            (self.bin / name).write_text('#!/bin/sh\nexit 1\n')
            os.chmod(self.bin / name, 0o755)
        self.discord = Path(self.temp.name) / 'discord.env'
        self.discord.write_text('DISCORD_BOT_TOKEN=synthetic\n')
        self.claude_config = Path(self.temp.name) / 'claude.json'
        self.claude_config.write_text(json.dumps({'projects': {str(self.root.parent): {'hasTrustDialogAccepted': True}}}))
        self.spawned, self.alive = [], True
        self.queue = self.dashboard(Host(spawn=self.spawn, alive=lambda pid, manifest: self.alive,
                                         python=str(self.root / '.venv/bin/python'),
                                         env={'PATH': str(self.bin), 'CLAUDECODE': '1', 'CLAUDE_CODE_MESSAGING_TOKEN': 't'},
                                         discord_env=self.discord, claude_config=self.claude_config))

    def dashboard(self, host):
        return DashboardQueue(self.root, self.f.db, claude_host=host)

    def spawn(self, argv, **kwargs):
        self.spawned.append({'argv': argv, **kwargs})
        return type('Process', (), {'pid': 515151})()

    def payload(self, ids=(1, 2), request='claude-one', runtime='claude'):
        payload = dict(request_id=request, items=[dict(real_job_id=str(1000 + i), source_job_id=i) for i in ids],
                       provenance=dict(kind='local_dashboard', origin='http://localhost:7654', received_at=now()),
                       authorize_preparation=True, backend_origin='http://127.0.0.1:7654', project_root=str(self.root))
        return payload | ({'runtime': runtime} if runtime else {})

    def manifest_path(self, request='claude-one'):
        return self.root / '.pipeline/batches' / f'dashboard-claude-{request}.json'

    def set_queue(self, job_id, state, owner=None):
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute('UPDATE application_queue SET state=?,codex_application_session_id=? WHERE job_id=?', (state, owner, job_id))

    def hold_worker_lock(self):
        path = self.root / '.pipeline/worker-progress.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def test_start_writes_a_claude_manifest_and_spawns_one_plain_worker(self):
        receipt = self.queue.handoff(self.payload())
        self.assertEqual((receipt['status'], receipt['runtime'], receipt['intent']), ('preparing', 'claude', 'handoff'))
        manifest = self.manifest_path()
        self.assertFalse(manifest.name.startswith('bridge-'))
        pipeline = BatchPipeline(PipelineConfig.discover(self.root), manifest)
        self.assertNotIn('application_runtime', json.loads(manifest.read_text()))
        self.assertEqual(pipeline.dashboard_url, 'http://127.0.0.1:7654')
        self.assertEqual(pipeline.target_ids, (1, 2))
        self.assertEqual(pipeline.candidate_snapshots[2]['jd_text'], 'Complete JD 2')
        self.assertEqual(len(self.spawned), 1)
        call = self.spawned[0]
        output = self.root / 'monitor-output/dashboard-claude-claude-one'
        self.assertEqual(call['argv'], [str(self.root / '.venv/bin/python'), '-m', 'pipeline.cli', '--root', str(self.root),
                                        'watch', '--batch', str(manifest), '--application-workers', '2',
                                        '--progress-output', str(output), '--progress-local-only'])
        self.assertTrue((output / 'worker.log').exists())
        self.assertEqual(call['env']['PATH'].split(os.pathsep), [str(self.root / '.venv/bin'), str(self.bin)])
        self.assertFalse({'CLAUDECODE', 'CLAUDE_CODE_MESSAGING_TOKEN'} & set(call['env']))
        self.assertTrue(call['start_new_session'])
        self.assertEqual(call['cwd'], self.root)
        launch = json.loads(manifest.with_name(manifest.stem + '-launch.json').read_text())
        self.assertEqual((launch['pid'], launch['argv']), (515151, call['argv']))
        with closing(sqlite3.connect(self.f.db)) as db:
            self.assertEqual(db.execute('SELECT job_id,state FROM application_queue ORDER BY job_id').fetchall(), [(1, 'Queued'), (2, 'Queued')])

    def test_replaying_the_same_request_spawns_nothing_new(self):
        first = self.queue.handoff(self.payload())
        self.assertEqual(self.queue.handoff(self.payload()), first)
        self.assertEqual(self.queue.handoff(self.payload(runtime=None)), first)
        self.assertEqual(len(self.spawned), 1)
        with self.assertRaisesRegex(ValueError, 'another selection'):
            self.queue.handoff(self.payload(ids=(1,)))

    def test_running_worker_blocks_start_without_writing_or_spawning(self):
        lock = self.hold_worker_lock()
        receipt = self.queue.handoff(self.payload())
        self.assertEqual(receipt['status'], 'waiting_for_capacity')
        self.assertEqual(receipt['reason'], 'Another application worker is running. Start these jobs after it stops.')
        self.assertFalse(self.manifest_path().exists())
        self.assertEqual(self.spawned, [])
        lock.close()
        self.assertEqual(self.queue.handoff(self.payload())['status'], 'preparing')
        self.assertEqual(len(self.spawned), 1)

    def test_a_just_launched_dashboard_worker_also_blocks_a_second_start(self):
        self.queue.handoff(self.payload(ids=(1,), request='first'))
        self.assertEqual(self.queue.handoff(self.payload(ids=(2,), request='second'))['status'], 'waiting_for_capacity')
        self.assertEqual(len(self.spawned), 1)

    def test_a_worker_whose_jobs_all_have_appliers_hands_over_to_the_new_start(self):
        self.queue.handoff(self.payload(ids=(1,), request='first'))
        interrupted = []

        def interrupt(pid):
            interrupted.append(pid)
            self.alive = False
        queue = self.dashboard(Host(spawn=self.spawn, alive=lambda pid, manifest: self.alive, interrupt=interrupt, sleep=lambda s: None,
                                    python=str(self.root / '.venv/bin/python'), env={'PATH': str(self.bin)},
                                    discord_env=self.discord, claude_config=self.claude_config))
        launcher = ClaudeApplierLauncher(self.root, run=FakeClaude().run)
        launcher.launch(1, scope=str(self.manifest_path('first')), url='https://example.test/1', materials=[], work_dir=self.root / 'work-1')
        receipt = queue.handoff(self.payload(ids=(2,), request='second'))
        self.assertTrue(self.manifest_path('second').exists(), receipt)
        self.assertEqual(interrupted, [515151])
        self.assertEqual(len(self.spawned), 2)

    def test_the_autopilot_worker_hands_over_even_with_a_job_still_to_start(self):
        from pipeline import autopilot
        from pipeline.local_state import private_json
        manifest = autopilot.write_manifest(self.root)
        data = json.loads(manifest.read_text())
        data['ordered_candidates'] = [{'id': 3, 'company': 'Fixture Company', 'title': 'Engineer 3', 'url': 'https://fixture.invalid/jobs/3', 'jd_text': 'JD'}]
        private_json(manifest, data, self.root)
        private_json(autopilot.launch_path(self.root), {'argv': autopilot.worker_command(self.root, 'python'), 'pid': 717171, 'started_at': now()}, self.root)
        interrupted = []
        queue = self.dashboard(Host(spawn=self.spawn, alive=lambda pid, manifest: pid not in interrupted, interrupt=interrupted.append,
                                    sleep=lambda s: None, python=str(self.root / '.venv/bin/python'), env={'PATH': str(self.bin)},
                                    discord_env=self.discord, claude_config=self.claude_config))
        self.assertEqual(queue.handoff(self.payload(ids=(1,), request='after-autopilot'))['status'], 'preparing')
        self.assertEqual(interrupted, [717171])
        self.assertEqual(len(self.spawned), 1)

    def test_a_start_whose_worker_the_autopilot_took_over_is_still_preparing(self):
        from pipeline import autopilot
        from pipeline.local_state import private_json
        self.queue.handoff(self.payload(ids=(1,), request='first'))
        private_json(autopilot.launch_path(self.root), {'argv': autopilot.worker_command(self.root, 'python'), 'pid': 717171, 'started_at': now()}, self.root)
        for running, status in (({717171}, 'preparing'), (set(), 'needs_attention')):
            queue = self.dashboard(Host(spawn=self.spawn, alive=lambda pid, manifest: pid in running, python=str(self.root / '.venv/bin/python'),
                                        env={'PATH': str(self.bin)}, discord_env=self.discord, claude_config=self.claude_config))
            self.assertEqual(queue.status('first')['requests'][0]['status'], status)

    def test_a_worker_with_a_job_still_to_start_keeps_the_new_start_waiting(self):
        self.queue.handoff(self.payload(ids=(1, 2), request='first'))
        launcher = ClaudeApplierLauncher(self.root, run=FakeClaude().run)
        launcher.launch(1, scope=str(self.manifest_path('first')), url='https://example.test/1', materials=[], work_dir=self.root / 'work-1')
        self.assertEqual(self.queue.handoff(self.payload(ids=(3,), request='second'))['status'], 'waiting_for_capacity')
        self.assertEqual(len(self.spawned), 1)

    def test_missing_setup_lists_every_item_and_starts_nothing(self):
        (self.root / 'application-profile.md').write_text('')
        (self.root / 'resume-catalog.json').write_text('{}')
        self.claude_config.write_text(json.dumps({'projects': {str(self.root.parent): {'hasTrustDialogAccepted': True},
                                                               str(self.root): {'hasTrustDialogAccepted': False}}}))
        queue = self.dashboard(Host(spawn=self.spawn, python=str(self.root / '.venv/bin/python'), env={'PATH': ''},
                                    discord_env=Path(self.temp.name) / 'absent.env', claude_config=self.claude_config))
        receipt = queue.handoff(self.payload())
        self.assertEqual(receipt['status'], 'needs_attention')
        for name in ('application-profile.md', 'resume-catalog.json', 'claude CLI', 'jobfeed CLI', 'Discord token file', 'Claude trust for'):
            self.assertIn(name, receipt['reason'])
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.manifest_path().exists())

    def test_a_job_claimed_before_start_is_named_and_nothing_starts(self):
        lock = self.hold_worker_lock()
        self.queue.handoff(self.payload())
        self.set_queue(2, 'Applying', 'other-owner')
        lock.close()
        receipt = self.queue.handoff(self.payload())
        self.assertEqual(receipt['status'], 'needs_attention')
        self.assertIn('Job 2 (Fixture Company · Engineer 2)', receipt['reason'])
        self.assertNotIn('Job 1', receipt['reason'])
        self.assertEqual(self.spawned, [])

    def test_spawn_failure_needs_attention_without_launch_record(self):
        def fail(argv, **kwargs):
            raise FileNotFoundError('python is missing')
        queue = self.dashboard(Host(spawn=fail, python=str(self.root / '.venv/bin/python'), env={'PATH': str(self.bin)}, discord_env=self.discord, claude_config=self.claude_config))
        receipt = queue.handoff(self.payload())
        self.assertEqual(receipt['status'], 'needs_attention')
        self.assertIn('python is missing', receipt['reason'])
        manifest = self.manifest_path()
        self.assertFalse(manifest.with_name(manifest.stem + '-launch.json').exists())

    def test_status_follows_the_jobs_and_the_worker(self):
        self.queue.handoff(self.payload())
        self.set_queue(1, 'Applying', 'a' * 32)
        self.assertEqual(self.queue.status('claude-one')['requests'][0]['status'], 'preparing')
        self.alive = False
        stopped = self.queue.status('claude-one')['requests'][0]
        self.assertEqual(stopped['status'], 'needs_attention')
        self.assertIn('worker stopped before these jobs finished', stopped['reason'])
        self.set_queue(1, 'Ready for Review', 'a' * 32)
        self.set_queue(2, 'Ready for Review', 'b' * 32)
        review = self.queue.status('claude-one')['requests'][0]
        self.assertEqual((review['status'], review['reason']), ('ready_for_review', 'Confirm Submit for each draft in Application runs when you are ready.'))
        self.set_queue(2, 'Needs Input', 'b' * 32)
        blocked = self.queue.status('claude-one')['requests'][0]
        self.assertEqual(blocked['status'], 'needs_attention')
        self.assertIn('Drafts are preserved', blocked['reason'])

    def test_jobs_recorded_as_canonical_applications_settle_the_request(self):
        self.queue.handoff(self.payload())
        with closing(sqlite3.connect(self.f.db)) as db, db:
            db.execute('DELETE FROM application_queue')
            db.execute('CREATE TABLE real_job_applications(id INTEGER PRIMARY KEY, source_job_id INTEGER, source_applied_job_id INTEGER)')
            db.executemany('INSERT INTO real_job_applications(source_job_id) VALUES(?)', [(1,), (2,)])
        done = self.queue.status('claude-one')['requests'][0]
        self.assertEqual((done['status'], done['reason']), ('ready_for_review', 'Every job in this start is submitted and recorded as applied.'))

    def launch_applier(self, job_id, owner):
        self.set_queue(job_id, 'Applying', owner)
        fake = FakeClaude()
        launcher = ClaudeApplierLauncher(self.root, run=fake.run, attach=fake.attach)
        launcher.launch(job_id, scope=str(self.manifest_path()), url=f'https://fixture.invalid/jobs/{job_id}', materials=[],
                        work_dir=self.root / f'claude-work-{job_id}', launch_id=owner)

    def test_status_is_ready_for_review_once_every_applier_records_it(self):
        self.queue.handoff(self.payload())
        store = ApplierRuntime(self.root)
        for job_id, owner in ((1, 'a' * 32), (2, 'b' * 32)):
            self.launch_applier(job_id, owner)
        store.progress(1, 'a' * 32, 'ready for review')
        store.progress(2, 'b' * 32, 'contact section')
        self.assertEqual(self.queue.status('claude-one')['requests'][0]['status'], 'preparing')
        store.progress(2, 'b' * 32, 'ready for review')
        self.assertEqual(self.queue.status('claude-one')['requests'][0]['status'], 'ready_for_review')
        self.set_queue(2, 'Applying', 'c' * 32)
        self.assertEqual(self.queue.status('claude-one')['requests'][0]['status'], 'preparing')

    def test_the_worker_writes_the_observation_that_runs_and_submit_read(self):
        from jobfeed.apply_health import ApplyHealthReader
        from pipeline import cli, worker_supervisor
        from pipeline.dashboard_submission import DashboardSubmissions
        self.queue.handoff(self.payload(ids=(1,)))
        owner = 'a' * 32
        self.launch_applier(1, owner)
        ApplierRuntime(self.root).progress(1, owner, 'ready for review')
        children = []

        class Worker:
            def __init__(self, pipeline, settings, *, progress_monitor):
                children.append(progress_monitor)

            def run_forever(self):
                children[-1].tick(force=True)

        def spawn(command, **kwargs):
            self.assertEqual(command[-1], '--worker-child')
            with patch('sys.argv', ['cli', *command[3:]]):
                cli.main()
            return Mock(poll=Mock(return_value=0), returncode=0)

        supervise = worker_supervisor.supervise
        with patch('sys.argv', ['cli', *self.spawned[0]['argv'][3:]]), patch('sys.stdout'), patch('pipeline.cli.OrchestratorWorker', Worker), \
                patch('pipeline.worker_supervisor.supervise', lambda *a, **k: supervise(*a, spawn=spawn, sleep=lambda _: None, **k)):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(children)
        batch = next(b for b in ApplyHealthReader(self.root).read(self.f.db).batches if b.id == 'dashboard-claude-claude-one')
        self.assertEqual((batch.freshness, batch.source_binding), ('fresh', 'valid'))
        self.assertEqual([(j.job_id, j.owner_thread_id, j.turn_state, j.result_status) for j in batch.jobs], [(1, owner, 'ended', 'ready_for_review')])
        self.assertEqual(DashboardSubmissions(PipelineConfig.discover(self.root)).resolve_scope(batch_id=batch.id), str(self.manifest_path()))

    def test_a_runtime_other_than_claude_is_rejected(self):
        for runtime in ('codex', 'gemini'):
            with self.assertRaisesRegex(ValueError, 'runtime must be claude'):
                self.queue.handoff(self.payload(request=runtime, runtime=runtime))
        self.assertEqual(self.spawned, [])


if __name__ == '__main__':
    unittest.main()
