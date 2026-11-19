import tempfile
import unittest
from pathlib import Path
from pipeline.decision_queue import DecisionQueue

T='2026-10-16T10:00:00+00:00'; L='2026-10-16T10:04:00+00:00'

class DecisionQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'q.sqlite';self.q=DecisionQueue(self.path,scope='batch-a',controller='control')
    def observe(self,job=1,field='email',evidence='screen1'):
        return self.q.observe_one(job=job,owner='owner',features={'step':'form','field':field,'gap':'required'},evidence=evidence,now=T)
    def claim(self):return self.q.next(T)
    def prepare(self,t,id='a'):
        return self.q.prepare(t['id'],t['claim_token'],t['issue_version'],id,'owner',{'operation':'recheck'},T)
    def send(self,t,id='a',transport=None):
        return self.q.send(t['id'],t['claim_token'],t['issue_version'],id,transport or (lambda *a:{'turn_id':'turn'}),T)
    def test_cancelled_job_removes_all_stale_blockers(self):
        self.observe(field='foster_youth')
        self.observe(field='references')
        self.q.db.execute("UPDATE dq_issues SET status='parked'")
        self.q.db.commit()
        self.q.observe([{'job_id':1,'paused':True,'queue_state':'Cancelled'}], [], L)
        self.assertEqual(self.q.parked(), [])
        self.assertEqual(self.q.pending_count(), 0)
        self.assertEqual(self.q.db.execute("SELECT state FROM dq_jobs WHERE job='1'").fetchone()[0], 'cancelled')

    def test_a_job_its_batch_stops_suspending_is_active_again_but_a_user_pause_stays(self):
        self.q.reconcile_membership([1, 2], T)
        self.q.observe([{'job_id':1,'paused':True,'queue_state':'Queued'}], [], T)
        self.q.cancel_job(2, 'paused', {'source':'user_update'}, T)
        self.q.observe([{'job_id':1,'queue_state':'Queued'}, {'job_id':2,'queue_state':'Queued'}], [], L)
        states = dict(self.q.db.execute("SELECT job,state FROM dq_jobs WHERE scope='batch-a'").fetchall())
        self.assertEqual((states['1'], states['2']), ('active', 'paused'))

    def test_terms_review_compares_existing_authorization_before_asking(self):
        self.q.observe_one(job=1,owner='owner',features={'field':'consent'},
                           evidence='current terms',kind='binding_terms',now=T)
        task = self.claim()
        self.assertIn('已接受的同类条款', task['instruction'])
        self.assertIn('授权范围', task['instruction'])
        self.assertIn('新增义务或范围差异', task['instruction'])

    def test_model_upgrade_stays_program_owned_until_actual_failure(self):
        self.q.observe([{'job_id':1,'owner':'owner','blocked':'needs_strong_model',
                         'result_status':'needs_strong_model'}],[],T)
        self.q.recover('2026-10-16T11:00:00+00:00')
        self.assertEqual(self.q.pending_count(),0)
        self.q.observe_one(job=1,owner='owner',features={'desktop_request':'upgrade-1'},
                           evidence={'error':'upgrade dispatch failed'},kind='desktop_delivery',now='2026-10-16T11:00:00+00:00')
        self.assertEqual(self.q.pending_count(),1)

    def test_ordinary_closeout_wait_does_not_become_controller_todo(self):
        self.q.observe([{'job_id':1,'owner':'owner','submitted':True,'synced':True}],[],T)
        self.q.recover('2026-10-16T11:00:00+00:00')
        self.assertEqual(self.q.pending_count(),0)
        self.q.observe_one(job=1,owner='owner',features={'desktop_request':'close-1'},
                           evidence={'error':'browser close failed'},kind='closeout',now='2026-10-16T11:00:00+00:00')
        self.assertEqual(self.q.pending_count(),1)

    def test_verify_reuses_same_version_evidence_despite_renamed_action(self):
        self.observe();t=self.claim()
        self.q.prepare(t['id'],t['claim_token'],1,'verify-a','owner',{'operation':'verify','text':'read terms'},T)
        self.send(t,'verify-a')
        self.q.report_execution('verify-a','owner','turn-a',{'quote':'saved terms'},T)
        self.q.accept('verify-a',1,'control',{'quote':'saved terms'},'failed',T)
        t=self.claim()
        with self.assertRaisesRegex(ValueError,'verification evidence already exists'):
            self.q.prepare(t['id'],t['claim_token'],1,'renamed','owner',{'operation':'verify','text':'rephrase'},T)
        issue=self.q.observe_one(job=1,owner='owner',features={'step':'form','field':'email','gap':'required'},
                                 evidence='screen1',turn='different-turn',now=L)
        self.assertEqual(issue['revision'],1)
        issue=self.q.observe_one(job=1,owner='owner',features={'step':'form','field':'email','gap':'required'},
                                 evidence='changed terms',turn='different-turn',now=L)
        self.assertEqual(issue['revision'],2)
        t=self.q.next(L)
        self.q.prepare(t['id'],t['claim_token'],2,'new-evidence','owner',{'operation':'verify','text':'check changed terms'},L)

    def test_01_human_categories_require_review(self):
        self.q.observe([{'job_id':1,'owner':'owner','blocked':'needs_input','blocker_kind':'captcha','result_quote':'captcha'}],[],T)
        self.assertEqual(self.q.parked(),[]);self.assertEqual(self.q.pending_count(),1)
    def test_02_user_decision_needs_sources_and_request(self):
        self.observe();t=self.claim()
        with self.assertRaises(ValueError):self.q.decide(t['id'],t['claim_token'],1,'user',{'page':'x'},T,{})
        self.assertEqual(self.q.parked(),[])
    def test_03_expired_claim_and_recoverable_recovery(self):
        self.observe();old=self.claim();self.q.recover(L);new=self.q.next(L)
        with self.assertRaises(ValueError):self.q.done(old['id'],'x',L,token=old['claim_token'],revision=1)
        self.assertEqual(new['phase'],'recover');self.q.recover('2026-10-16T10:08:00+00:00')
        self.assertEqual(self.q.pending_count(),1)
    def test_04_identity_not_screenshot_or_category(self):
        one=self.observe();self.observe(evidence='screen2');self.observe(field='phone')
        self.assertEqual(self.q.pending_count(),2)
        self.assertEqual(self.observe(evidence='screen2')['id'],one['id'])
    def test_05_delivered_is_not_verified(self):
        self.observe();t=self.claim();self.prepare(t);self.send(t)
        self.q.recover(L);self.assertEqual(self.q.next(L)['phase'],'inspect_execution')
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'waiting')
    def test_06_retry_budget_survives_renaming(self):
        self.observe()
        for n in range(3):
            t=self.claim();self.prepare(t,str(n));self.send(t,str(n));self.q.report_execution(str(n),'owner',str(n),{'error':'same'},T)
            self.q.accept(str(n),1,'control',{'error':'verified'},'failed',T)
            # discard acceptance todo because controller already performed acceptance
            self.q.db.execute("UPDATE dq_todos SET status='done' WHERE phase='accept'")
        t=self.claim()
        with self.assertRaises(ValueError):self.prepare(t,'renamed')
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'diagnostic')
        self.observe(job=2);self.assertEqual(self.q.next(T)['job_id'],2)
    def test_07_scope_missing_and_partial_not_resolution(self):
        i=self.observe();self.q.observe([],[],T);self.assertEqual(self.q._issue(i['id'])['status'],'review')
        b=DecisionQueue(self.path,scope='batch-b',controller='b');self.assertIsNone(b.next(T))
        self.q.observe_one(job=1,owner='owner',features={},evidence='read failed',now=T,complete=False)
        self.assertEqual(self.q.pending_count(),1)
    def test_08_cancel_fences_prepared_and_unknown_never_resends(self):
        self.observe();t=self.claim();self.prepare(t)
        self.q.cancel_job(1,'completed',{'message_id':'human'},T)
        calls=[]
        with self.assertRaises(ValueError):self.send(t,transport=lambda *a:calls.append(a))
        self.assertFalse(calls)
    def test_09_heartbeat_coalesces_and_idle_race(self):
        self.assertIsNone(self.q.heartbeat_claim(T));self.observe();wake=self.q.heartbeat_claim(T)
        self.assertTrue(wake);self.assertEqual(self.q.heartbeat_claim(T),wake);self.q.heartbeat_start(wake,T);self.assertIsNone(self.q.heartbeat_claim(T));self.q.heartbeat_ack(wake,T)
        self.q.controller_idle(T);self.assertTrue(self.q.heartbeat_claim(T))
    def test_10_request_same_screenshot_and_answer_cancels(self):
        self.observe();t=self.claim();r=dict(action='Solve captcha',evidence='page',checked_sources=['profile'],why_unavailable='human',semantic_request='captcha',applicability='company')
        self.q.decide(t['id'],t['claim_token'],1,'user',{'page':'captcha'},T,r)
        self.observe(evidence='new screenshot');self.assertEqual(len(self.q.parked()),1)
        self.q.user_answer(1,{'verified_human':True,'message_id':'m'},T)
        self.assertEqual(self.q.deliver_requests(lambda *a:self.fail('obsolete send'),T),[])
    def test_12_send_unknown_replay_no_duplicate(self):
        self.observe();t=self.claim();self.prepare(t);calls=[]
        def lost(*args):calls.append(args);raise RuntimeError('receipt lost')
        self.assertEqual(self.send(t,transport=lost)['state'],'unknown')
        self.q.recover(L);r=self.q.next(L)
        self.assertEqual(r['phase'],'reconcile')
        self.assertEqual(self.q.prepare(r['id'],r['claim_token'],1,'a','owner',{'operation':'recheck'},L)['state'],'unknown')
        self.assertEqual(len(calls),1)
    def test_13_owner_cannot_close(self):
        self.observe();t=self.claim();self.prepare(t);self.send(t);self.q.report_execution('a','owner','turn',{'claim':'done'},T)
        with self.assertRaises(ValueError):self.q.accept('a',1,'owner',{'claim':'done'},'resolved',T)
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'waiting')
        self.q.accept('a',1,'program',{'native_receipt':'verified'},'resolved',T)
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'closed')
    def test_migration_quarantines_unknown_claims(self):
        import sqlite3
        path=Path(self.tmp.name)/'legacy.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE issues(id INTEGER,job_id INTEGER,status TEXT)');db.execute("INSERT INTO issues VALUES(1,7,'waiting')")
        q=DecisionQueue(path,scope='new',controller='new');self.assertEqual(q.pending_count(),0)
        self.assertEqual(q.db.execute('SELECT status FROM dq_issues').fetchone()[0],'diagnostic')

    def test_unknown_cannot_be_resent_by_new_action_id(self):
        self.observe(); t=self.claim(); self.prepare(t)
        def lost(*args): raise RuntimeError('unknown')
        self.send(t,transport=lost); self.q.recover(L); recovered=self.q.next(L)
        with self.assertRaisesRegex(ValueError,'unresolved action'):
            self.q.prepare(recovered['id'],recovered['claim_token'],1,'renamed','owner',{'operation':'recheck'},L)

    def test_reconciled_unknown_resumes_evidence_tracking(self):
        self.observe(); t=self.claim(); self.prepare(t)
        def lost(*args): raise RuntimeError('unknown')
        self.send(t,transport=lost); self.q.recover(L)
        self.q.reconcile('a',{'turn_id':'actual-turn'},L)
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'waiting')
        self.assertEqual(self.q.pending_count(),0)
        self.q.recover('2026-10-16T10:08:00+00:00')
        self.assertEqual(self.q.next('2026-10-16T10:08:00+00:00')['phase'],'inspect_execution')

    def test_cancelled_inflight_report_cannot_revive_work(self):
        self.observe(); t=self.claim(); self.prepare(t); self.send(t)
        self.q.cancel_job(1,'paused',{'message_id':'stop'},T)
        self.q.report_execution('a','owner','late-turn',{'native_receipt':'late'},T)
        self.assertEqual(self.q.pending_count(),0)
        with self.assertRaises(ValueError):
            self.q.accept('a',1,'program',{'native_receipt':'verified'},'resolved',T)
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'cancelled')

    def test_accepted_execution_replay_preserves_original_evidence_and_verdict(self):
        self.observe(); task = self.claim(); self.prepare(task); self.send(task)
        self.q.report_execution('a', 'owner', 'turn', {'original': 'evidence'}, T)
        self.q.accept('a', 1, 'control', {'independent': 'verified'}, 'resolved', T)
        before = {table: [tuple(row) for row in self.q.db.execute('SELECT * FROM ' + table)]
                  for table in ('dq_executions', 'dq_actions', 'dq_issues', 'dq_todos', 'dq_audit')}
        result = self.q.report_execution('a', 'owner', 'turn', {'source': 'later official read'}, L)
        self.assertEqual(result, {'action_state': 'verified', 'already_recorded': True})
        after = {table: [tuple(row) for row in self.q.db.execute('SELECT * FROM ' + table)] for table in before}
        self.assertEqual(after, before)

    def test_execution_replay_rejects_conflicting_owner_or_turn(self):
        self.observe(); task = self.claim(); self.prepare(task); self.send(task)
        self.q.report_execution('a', 'owner', 'turn', {'original': 'evidence'}, T)
        for owner, turn in [('other', 'turn'), ('owner', 'different-turn')]:
            with self.subTest(owner=owner, turn=turn), self.assertRaises(ValueError):
                self.q.report_execution('a', owner, turn, {'new': 'evidence'}, L)
        self.assertEqual(self.q.db.execute('SELECT turn FROM dq_executions').fetchone()[0], 'turn')

    def test_terminal_action_without_recorded_execution_is_not_reconciled(self):
        self.observe(); task = self.claim(); self.prepare(task); self.send(task)
        self.q.db.execute("UPDATE dq_actions SET state='verified' WHERE id='a'")
        with self.assertRaises(ValueError):
            self.q.report_execution('a', 'owner', 'turn', {'new': 'evidence'}, L)

    def test_stale_execution_requires_current_evidence_revalidation(self):
        self.observe(); t=self.claim(); self.prepare(t); self.send(t)
        self.observe(evidence='new-screen')
        self.q.report_execution('a','owner','old-turn',{'native_receipt':'old'},T)
        self.assertEqual(self.q.pending_count(),1)
        with self.assertRaises(ValueError):
            self.q.accept('a',2,'control',{'receipt':'old'},'resolved',T)
        self.q.accept('a',2,'control',{'receipt':'current verified'},'resolved',T, revalidation={'action_revision':1,'current_revision':2,'current_evidence':'new-screen','basis':'same native receipt confirms current question'})
        self.assertEqual(self.q._issue(t['issue_id'])['status'],'closed')

    def test_failure_acceptance_consumes_todo_and_is_terminal(self):
        self.observe(); t=self.claim(); self.prepare(t); self.send(t)
        self.q.report_execution('a','owner','turn',{'error':'actual'},T)
        self.q.accept('a',1,'control',{'error':'verified'},'failed',T)
        self.assertEqual(self.q.pending_count(),1)
        self.assertEqual(self.q.next(T)['phase'],'review')
        self.q.accept('a',1,'control',{'error':'verified'},'failed',T)
        with self.assertRaisesRegex(ValueError,'conflicting terminal'):
            self.q.accept('a',1,'control',{'receipt':'contradiction'},'resolved',T)

    def test_old_controller_cannot_reset_or_ack_new_controller_wake(self):
        self.observe(); wake=self.q.heartbeat_claim(T)
        old=DecisionQueue(self.path,scope='batch-a',controller='wrong-controller')
        with self.assertRaises(ValueError):old.heartbeat_ack(wake,T)
        with self.assertRaises(ValueError):old.controller_idle(T)
        self.assertEqual(self.q.heartbeat_claim(T), wake)


    def test_terminal_controller_rearms_wake_after_crash(self):
        self.observe();wake=self.q.heartbeat_claim(T)
        self.q.heartbeat_start(wake,T);self.q.heartbeat_ack(wake,T)
        self.claim();self.q.recover(L)
        self.assertIsNone(self.q.heartbeat_claim(L))
        with self.assertRaises(ValueError):self.q.heartbeat_terminal(wake,{'status':'unknown'},L)
        self.q.heartbeat_terminal(wake,{'turn_id':'controller-old','status':'interrupted'},L)
        self.assertNotEqual(self.q.heartbeat_claim(L),wake)

    def test_new_answer_between_prepare_and_send_retires_unsent_action(self):
        self.observe();t=self.claim();self.prepare(t)
        self.q.user_answer(1,{'verified_human':True,'message_id':'human-new','text':'actual answer'},T)
        self.assertEqual(self.q.db.execute("SELECT state FROM dq_actions WHERE id='a'").fetchone()[0],'cancelled')
        newer=self.claim();self.prepare(newer,'new')
        with self.assertRaises(ValueError):self.send(t)
        self.send(newer,'new')

    def test_a_blocker_seen_again_unchanged_adds_nothing_to_its_history(self):
        for _ in range(5):
            self.observe()
        self.assertEqual(self.q.db.execute("SELECT count(*) FROM dq_audit WHERE event='observation'").fetchone()[0], 1)
        self.observe(evidence='screen2')
        self.assertEqual(self.q.db.execute("SELECT count(*) FROM dq_audit WHERE event='observation'").fetchone()[0], 2)

    def test_a_todo_carries_only_its_latest_events(self):
        issue = self.observe()['id']
        for n in range(DecisionQueue.HISTORY + 5):
            self.q._audit('decision:retry', issue, {'n': n}, T)
        self.q.db.commit()
        history = self.claim()['history']
        self.assertEqual(len(history), DecisionQueue.HISTORY)
        self.assertEqual((history[0]['summary'], history[-1]['summary']),
                         ('decision:retry: {"n": 5}', f'decision:retry: {{"n": {DecisionQueue.HISTORY + 4}}}'))

    def test_new_observation_retires_unsent_action(self):
        self.observe();t=self.claim();self.prepare(t)
        self.observe(evidence='new-current-page')
        newer=self.claim();self.prepare(newer,'new')
        self.assertEqual(self.q.db.execute("SELECT state FROM dq_actions WHERE id='a'").fetchone()[0],'cancelled')
