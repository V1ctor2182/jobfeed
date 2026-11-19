from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from confirmation_fixture import write_confirmation
from test_claude_applier import FakeClaude

from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.progress_monitor import LocalSources, ProgressMonitor, locked
from pipeline.worker import OrchestratorWorker, WorkerSettings

NOW = '2026-10-13T18:00:00+00:00'
OWNERS = {1: '1' * 32, 2: '2' * 32}


@contextmanager
def clock(at):
    with patch('pipeline.applier_runtime._now', return_value=at), patch('pipeline.claude_applier._now', return_value=at):
        yield


def stop_with_blocker(f, job=1):
    """The Applier waits on a check only the user can do; success notices stay local since 2026-10-15."""
    f.ask(job, 'blocker', '需要验证', blocker_type='user_only_check')


class Fixture:
    def __init__(self, base):
        self.root = (base / 'source').resolve()
        self.root.mkdir()
        self.manifest = self.root / 'batch.json'
        self.data = dict(ordered_candidates=[dict(id=1, company='Acme', title='Engineer'), dict(id=2, company='Other', title='Engineer')],
                         target_successful_submissions=2, max_parallel=2, suspended_job_ids=[2])
        self.manifest.write_text(json.dumps(self.data))
        self.db = base / 'jobfeed.sqlite'
        with sqlite3.connect(self.db) as db:
            db.executescript('CREATE TABLE application_queue(job_id INTEGER, state TEXT, application_folder_relpath TEXT, codex_application_session_id TEXT);'
                             'CREATE TABLE jobs(id INTEGER, url TEXT, company TEXT); CREATE TABLE applied(job_id INTEGER);'
                             "INSERT INTO jobs(id,url) VALUES(1,'https://example.com/jobs/1'),(2,'https://example.com/jobs/2');")
            db.executemany('INSERT INTO application_queue VALUES(?,?,?,?)', [(1, 'Applying', 'one', OWNERS[1]), (2, 'Needs Input', 'two', OWNERS[2])])
        self.base = base
        self.fake = FakeClaude()
        self.launcher = ClaudeApplierLauncher(self.root, run=self.fake.run, attach=self.fake.attach)
        with clock(NOW):
            for job, owner in OWNERS.items():
                self.launcher.launch(job, scope=str(self.manifest), url=f'https://example.com/jobs/{job}', materials=[],
                                     work_dir=base / f'work-{job}', launch_id=owner)
        self.sources = LocalSources(self.root, self.manifest, self.db)
        self.monitor = ProgressMonitor(self.sources, base / 'output')

    def ask(self, job, kind, text, at=NOW, **fields):
        with clock(at):
            return self.launcher.store.ask(job, OWNERS[job], kind, text, **fields)

    def step(self, job, step, at=NOW):
        with clock(at):
            self.launcher.store.progress(job, OWNERS[job], step)

    def end(self, job, cli_state='done', at=NOW):
        """The Applier's session ends without a result: done finishes it, stopped aborts it."""
        next(a for a in self.fake.agents if a['name'].startswith(f'applier-{job}-'))['state'] = cli_state
        with clock(at):
            self.launcher.reconcile(OWNERS[job])

    def receipt(self, job=1, **fields):
        folder = 'one' if job == 1 else 'two'
        path = self.root / 'applications' / folder / 'submission-receipt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = write_confirmation(path.parent, job, f'https://example.com/jobs/{job}') if fields.get('success_evidence') == 'Application submitted' else {'job_id': job}
        data.update(fields)
        path.write_text(json.dumps(data))

    def close(self, job=1):
        self.receipt(job, success_evidence='Application submitted', tabs_closed=True, close_result=dict(status='already_applied',reason='标签关闭：完成，已枚举核验原标签不存在'))
        with sqlite3.connect(self.db) as db:
            db.execute('INSERT INTO applied VALUES(?)', (job,))
        path = self.root / '.pipeline/submission-closeouts' / f'{job}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(job_id=job, folder='one' if job == 1 else 'two', thread_id=OWNERS[job], complete=True)))
        self.end(job)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    def scan(self):
        return self.f.monitor.tick(force=True, now=NOW)

    def test_stalled_owner_stays_counted_without_halting_other_jobs(self):
        snapshot = self.f.monitor.tick(force=True, now='2026-10-13T18:20:00+00:00')
        self.assertTrue(any('no_recent_tool_activity' in e for e in snapshot['errors']))
        self.assertEqual(snapshot['dispatch_errors'], [])
        self.assertEqual(snapshot['counts']['occupied'], 2)

    def test_user_submission_overrides_stale_block_without_fabricating_receipt(self):
        self.f.receipt()
        path = self.f.root / 'applications/one/user-submission-confirmation.json'
        path.write_text(json.dumps(dict(job_id=1, status='submitted_user_confirmed',
            do_not_reapply=True, user_text='我提交了', recorded_at=NOW)))
        with sqlite3.connect(self.f.db) as db:
            db.execute("UPDATE application_queue SET state='Submission Unknown' WHERE job_id=1")
        job = self.scan()['jobs'][0]
        self.assertTrue(job['user_submitted'])
        self.assertEqual(job['blocked'], 'submission_unknown')
        self.assertFalse(job['submitted'])
        self.assertFalse(job['synced'])
        self.assertIn('核查提交结果', job['next_step'])
        path.write_text(json.dumps(dict(job_id=1, status='submitted_user_confirmed')))
        self.assertFalse(self.scan()['jobs'][0]['user_submitted'])

    def test_dispatch_failure_visible_without_blocking_recovery(self):
        error = '1:claude_dispatch_error:existing_session_requires_recovery_check'
        self.f.monitor.runtime_errors['1'] = error
        snapshot = self.scan()
        self.assertIn(error, snapshot['errors'])
        self.assertNotIn(error, snapshot['dispatch_errors'])
        self.f.monitor.runtime_errors.pop('1')
        self.assertNotIn(error, self.scan()['errors'])

    def test_paused_and_terminal_are_not_filtered(self):
        s = self.scan()
        self.assertEqual(len(s['jobs']), 2)
        self.assertEqual(s['counts']['paused'], 1)
        self.assertEqual(s['counts']['submitted'], 0)
        self.assertEqual(s['counts']['reserved'], 2)  # Paused but actually still executing.

    def test_three_independent_evidence_phases(self):
        self.f.receipt(success_evidence='Application submitted')
        s = self.scan()
        self.assertEqual((s['counts']['submitted'], s['counts']['synced'], s['counts']['closed']), (1, 0, 0))
        with sqlite3.connect(self.f.db) as db:
            db.execute('INSERT INTO applied VALUES(1)')
        self.assertEqual(self.scan()['counts']['closed'], 0)
        self.f.close()
        self.assertEqual(self.scan()['counts']['closed'], 1)

    def test_account_creation_not_success(self):
        self.f.receipt(success_evidence='Account Successfully Created')
        s = self.scan()
        self.assertEqual(s['counts']['submitted'], 0)
        self.assertIn('1:missing_submission_evidence', s['errors'])

    def test_identity_mismatch_is_unknown(self):
        self.f.receipt(success_evidence='Application submitted')
        p = self.f.root / 'applications/one/submission-receipt.json'
        p.write_text('{"job_id":99,"success_evidence":"yes"}')
        self.assertEqual(self.scan()['counts']['submitted'], 0)

    def test_unknown_submit_keeps_reservation(self):
        self.f.step(2, 'submitted')
        with sqlite3.connect(self.f.db) as db:
            db.execute("UPDATE application_queue SET state='Submission Unknown' WHERE job_id=2")
        self.assertTrue(self.scan()['jobs'][1]['reserved'])

    def test_read_errors_do_not_reuse_stale_success(self):
        self.f.close()
        self.scan()
        self.f.db.unlink()
        s = self.scan()
        self.assertEqual(s['counts']['synced'], 0)
        self.assertTrue(s['errors'])
        self.assertFalse(s['complete'])

    def test_restart_dedup_no_change_report_and_no_business_writes(self):
        # 阻碍台账 is the program's own record under .pipeline; everything else stays untouched.
        before = {p: p.read_bytes() for p in self.f.root.rglob('*') if p.is_file() and p.name != 'blockers.sqlite3'}
        self.scan()
        self.f.receipt(success_evidence='Application submitted')
        self.scan()
        state = json.loads(self.f.monitor.path.read_text())
        successes = [e for e in state['events'].values() if e['kind'] == '新增提交成功']
        self.assertEqual(len(successes), 1)
        monitor = ProgressMonitor(self.f.sources, self.f.monitor.output)
        monitor.tick(force=True, now='2026-10-13T18:02:00+00:00')
        state2 = json.loads(monitor.path.read_text())
        self.assertEqual(state['events'], state2['events'])
        self.assertEqual(state2['model_call_attempts'], 0)
        self.assertIn('18:02:00', (monitor.output / 'latest.md').read_text())
        for p, content in before.items():
            self.assertEqual(p.read_bytes(), content)

    def test_output_and_lock_isolation(self):
        with self.assertRaises(ValueError):
            ProgressMonitor(self.f.sources, self.f.root / '.pipeline/monitor')
        with locked(self.f.monitor.output):
            with self.assertRaises(BlockingIOError):
                self.scan()

    def test_complete_requires_all_closeouts_no_unknowns(self):
        self.scan()
        self.f.close(1)
        self.assertFalse(self.scan()['complete'])
        self.f.close(2)
        self.assertTrue(self.scan()['complete'])
        state = json.loads(self.f.monitor.path.read_text())
        self.assertEqual(len([e for e in state['events'].values() if e['kind'] == '整批完成']), 1)

    def test_replay_does_not_read_future_receipts(self):
        # Chronological fixture replay; explicitly synthetic intermediate artifacts.
        snapshots = [self.scan()]
        self.f.receipt(success_evidence='Application submitted')
        snapshots.append(self.scan())
        self.f.close(1)
        snapshots.append(self.scan())
        self.assertEqual([s['counts']['submitted'] for s in snapshots], [0, 1, 1])
        self.assertEqual([s['counts']['closed'] for s in snapshots], [0, 0, 1])

    def test_worker_guard_counts_pending_submission_and_holds_paused_job(self):
        from types import SimpleNamespace
        w = OrchestratorWorker(SimpleNamespace(), WorkerSettings(), progress_monitor=self.f.monitor)
        self.addCleanup(w.close)
        w._progress_snapshot = self.scan()
        self.assertFalse(w._progress_allows('application_started', 3))
        self.assertFalse(w._progress_allows('application_sync', 2))

    def test_corrupt_state_fails_closed(self):
        self.scan()
        self.f.monitor.path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.scan()


class AdditionalProgressTests(unittest.TestCase):
    setUp = ProgressTests.setUp
    scan = ProgressTests.scan
    def test_initial_blocker_emits_without_success_burst(self):
        stop_with_blocker(self.f)
        self.scan()
        events = json.loads(self.f.monitor.path.read_text())['events']
        self.assertEqual([e['kind'] for e in events.values()], ['待办唤醒'])

    def test_company_limit_blocks_new_owner(self):
        from types import SimpleNamespace
        w = OrchestratorWorker(SimpleNamespace(), WorkerSettings(), progress_monitor=self.f.monitor)
        self.addCleanup(w.close)
        snap = self.scan()
        snap['target'] = 10
        snap['max_per_company'] = 1
        snap['jobs'][1].update(company_key='acme', paused=False, owner=None)
        w._progress_snapshot = snap
        self.assertFalse(w._progress_allows('application_started', 2))

    def test_changed_manifest_stops_worker_dispatch(self):
        from types import SimpleNamespace
        w = OrchestratorWorker(SimpleNamespace(config=SimpleNamespace(resume_root=self.f.root)), WorkerSettings(), progress_monitor=self.f.monitor, emit=lambda e:None)
        self.addCleanup(w.close)
        w._check_progress()
        self.f.data['max_parallel'] = 1
        self.f.manifest.write_text(json.dumps(self.f.data))
        self.f.monitor._next_poll = 0
        w._check_progress()
        self.assertIsNone(w._progress_snapshot)
        self.assertFalse(w._progress_allows('dispatch'))

    def test_success_list_and_object_claims_require_artifacts(self):
        for evidence in [['Application submitted'], {'status': 'Application submitted'}]:
            self.f.receipt(success_evidence=evidence)
            self.assertEqual(self.scan()['counts']['submitted'], 0)

    def test_partial_source_failure_does_not_hide_other_jobs(self):
        self.f.close(1)
        with sqlite3.connect(self.f.db) as db:
            db.execute("UPDATE application_queue SET codex_application_session_id='owner-2' WHERE job_id=2")
        snap = self.scan()
        self.assertEqual(snap['counts']['closed'], 1)
        self.assertIn('2:session_unreadable', snap['errors'])

class CloseEvidenceCompatibilityTests(unittest.TestCase):
    setUp = ProgressTests.setUp
    scan = ProgressTests.scan
    def test_direct_closeout_evidence_schema(self):
        self.f.close(1)
        receipt = self.f.root / 'applications/one/submission-receipt.json'
        closeout = self.f.root / '.pipeline/submission-closeouts/1.json'
        r = json.loads(receipt.read_text()); r.pop('close_result')
        r['close_evidence'] = 'Closed original tab; tabs.list confirms absent.'
        c = json.loads(closeout.read_text()); c.update(tabs_closed=True,close_evidence=r['close_evidence'])
        receipt.write_text(json.dumps(r)); closeout.write_text(json.dumps(c))
        self.assertEqual(self.scan()['counts']['closed'], 1)
        c.pop('close_evidence'); closeout.write_text(json.dumps(c))
        self.assertEqual(self.scan()['counts']['closed'], 0)

if __name__ == '__main__':
    unittest.main()

class TimestampedCloseTests(unittest.TestCase):
 def test_timestamp_alone_is_not_close_proof(self):
  from pipeline.progress_monitor import timestamped_close_proof
  receipt=dict(closed_tab_ids=['123'],closed_verified_at='2026-10-13T20:00:00Z')
  cursor=dict(owner_verified=True,result_close_confirmed=True,result_at='2026-10-13T20:00:02Z',last_tool_at='2026-10-13T20:00:01Z')
  self.assertTrue(timestamped_close_proof(receipt,cursor))
  for key in ('owner_verified','result_close_confirmed','last_tool_at'):
   bad=dict(cursor);bad.pop(key)
   self.assertFalse(timestamped_close_proof(receipt,bad))
  receipt['closed_verified_at']='2026-10-14T20:00:00Z'
  self.assertFalse(timestamped_close_proof(receipt,cursor))
class StructuredCloseTests(unittest.TestCase):
 def test_target_and_visa_schemas_require_matching_tab_and_absence(self):
  from pipeline.progress_monitor import structured_close_proof
  for evidence in [dict(method='browser.user.openTabs before/after',before=dict(id='123'),after_id_absent=True),
                   dict(method='official Chrome browser.user.openTabs',before=[dict(id='123')],closed_tab_ids=['123'],after_target_present=False)]:
   r=dict(tab_id='123',tab_close_evidence=evidence)
   self.assertTrue(structured_close_proof(r))
   r['tab_id']='other'
   self.assertFalse(structured_close_proof(r))
  self.assertFalse(structured_close_proof(dict(tab_id='123',tab_close_evidence=dict(method='openTabs',before=dict(id='123')))))

class MonitorRepairTests(unittest.TestCase):
    def test_closed_job_retains_owner_after_queue_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(Path(tmp))
            with sqlite3.connect(f.db) as db:
                db.execute('DELETE FROM application_queue WHERE job_id=1')
            path = f.root / '.pipeline/closed-jobs/1.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(job_id=1, complete=True, evidence='Official posting closed', thread_id=OWNERS[1], folder=str(f.root / 'applications/one'))))
            s = f.monitor.tick(force=True, now=NOW)
            j = s['jobs'][0]
            self.assertTrue(j['terminal'])
            self.assertEqual(j['owner'], OWNERS[1])
            self.assertEqual(j['queue_state'], 'Unavailable')
            self.assertFalse(j['reserved'])
            self.assertIn('禁止重投', j['next_step'])
            self.assertIn('保留原页', j['next_step'])
            self.assertNotIn('保留页面', j['next_step'])

    def test_followup_errors_visible_but_not_dispatch_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(Path(tmp))
            f.data['ordered_candidates'] = [f.data['ordered_candidates'][0]]
            f.data['monitor_followup_file'] = 'followup.json'
            (f.root / 'followup.json').write_text(json.dumps({'candidates':[{'id':2,'company':'Old'}]}))
            f.manifest.write_text(json.dumps(f.data))
            with sqlite3.connect(f.db) as db:
                db.execute('INSERT INTO applied VALUES(2)')
            s = f.monitor.tick(force=True, now=NOW)
            self.assertIn('2:missing_submission_evidence', s['errors'])
            self.assertNotIn('2:missing_submission_evidence', s['dispatch_errors'])
            self.assertEqual(s['counts']['synced'], 0)
            self.assertFalse(next(j for j in s['jobs'] if j['job_id']==2)['counted'])
            self.assertTrue((f.monitor.output / 'dashboard.html').exists())

    def test_global_proof_requires_identity_and_global_inventory(self):
        from pipeline.progress_monitor import global_close_proof
        receipt = dict(tab_id='123', tabs_closed_evidence=dict(tab_id='123',method='browser.user.openTabs global',original_tab_absent=True,checked_at=NOW))
        self.assertTrue(global_close_proof(receipt))
        receipt['tabs_closed_evidence']['method']='browser.tabs.list'
        self.assertFalse(global_close_proof(receipt))
        receipt['tabs_closed_evidence']['method']='browser.user.openTabs'
        receipt['tabs_closed_evidence']['tab_id']='999'
        self.assertFalse(global_close_proof(receipt))


class AdditionalGlobalCloseProofTests(unittest.TestCase):
    def test_cgi_proof_requires_matching_tab_and_global_method(self):
        from pipeline.progress_monitor import global_close_proof
        r = dict(tab_id='123', tabs_closed_at=NOW, tab_close_evidence=dict(tab_id='123', before_url='https://example.com/success', verification_method='browser.user.openTabs', absent_after_close=True))
        self.assertTrue(global_close_proof(r))
        for key, value in [('tab_id','999'), ('verification_method','browser.tabs.list'), ('absent_after_close',False)]:
            bad=copy.deepcopy(r);bad['tab_close_evidence'][key]=value
            self.assertFalse(global_close_proof(bad))

    def test_already_absent_tabs_count_as_closed(self):
        # 2026-10-15 KGSTech 509327: the page was gone before cleanup, so no tab id was closed.
        from pipeline.progress_monitor import global_close_proof
        r = dict(tabs_closed=True, closed_tab_ids=[], tab_close_evidence=dict(
            method='browser.user.openTabs', confirmed_absent_tab_ids=['71010612'],
            global_checks=[dict(checked_at=NOW, matching_tabs=[]), dict(checked_at=NOW, matching_tabs=[])]))
        self.assertTrue(global_close_proof(r))
        for key, value in [('confirmed_absent_tab_ids', []), ('method', 'browser.tabs.list'), ('global_checks', []),
                           ('global_checks', [dict(checked_at=NOW, matching_tabs=[{'id': '71010612'}])])]:
            bad = copy.deepcopy(r); bad['tab_close_evidence'][key] = value
            self.assertFalse(global_close_proof(bad))

    def test_pokee_global_absence_requires_named_targets_and_actual_empty_after(self):
        from pipeline.progress_monitor import global_close_proof
        r=dict(tab_cleanup_evidence=dict(target_tab_ids=['123','456'], method='official browser.user.openTabs global listing', target_ids_present_after=[], checked_at=NOW))
        self.assertTrue(global_close_proof(r))
        for key,value in [('target_tab_ids',[]),('target_ids_present_after',['456']),('method','browser.tabs.list'),('checked_at',None)]:
            bad=copy.deepcopy(r);bad['tab_cleanup_evidence'][key]=value
            self.assertFalse(global_close_proof(bad))

class TerminalNotificationRegression(unittest.TestCase):
    setUp=ProgressTests.setUp
    scan=ProgressTests.scan
    def test_cancel_history_prevents_waiting_dispatch_after_queue_deletion(self):
        with sqlite3.connect(self.f.db) as db:
            db.executescript(f"CREATE TABLE application_terminal(job_id INTEGER,state TEXT,application_folder_relpath TEXT,codex_application_session_id TEXT); INSERT INTO application_terminal VALUES(1,'Cancelled','one','{OWNERS[1]}'); DELETE FROM application_queue WHERE job_id=1;")
        j=self.scan()['jobs'][0]
        self.assertTrue(j['terminal']);self.assertFalse(j['reserved'])
        self.assertNotIn('等待现有 worker',j['next_step'])
    def test_ended_without_receipt_is_one_todo_and_one_wake(self):
        self.f.end(1)
        self.scan();self.scan()
        state=json.loads((self.f.monitor.output/'monitor.json').read_text())
        wakes=[e for e in state['events'].values() if e['kind']=='待办唤醒']
        self.assertEqual(len(wakes),1);self.assertEqual(wakes[0]['delivery'],'pending')
        todo=self.f.monitor.ledger.next(NOW)
        self.assertEqual((todo['job_id'],todo['kind']),(1,'ended_without_receipt'))
        self.assertIsNone(self.f.monitor.ledger.next(NOW))

class ClosedInterruptedRegression(unittest.TestCase):
    setUp=ProgressTests.setUp
    scan=ProgressTests.scan
    def test_verified_closeout_does_not_occupy_slot_from_old_abort(self):
        self.f.close(1);self.f.close(2)
        self.f.end(1,'stopped')
        s=self.scan();self.assertEqual(s['counts']['occupied'],0)
