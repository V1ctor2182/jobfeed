"""阻碍台账 scenarios through its four operations; nothing here reads internal rows."""
from pathlib import Path
import tempfile
import unittest

from pipeline.blocker_ledger import LegacyBlockerLedger

T0 = '2026-10-16T10:00:00+00:00'


def at(minutes, seconds=0):
    """Timestamp minutes (and seconds) after T0."""
    total = minutes * 60 + seconds
    return f'2026-10-16T{10 + total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}+00:00'


def job(**changes):
    base = dict(job_id=1, company='Acme', title='Engineer', owner='owner-1', turn_state='ended', turn_at=T0,
                last_tool_at=T0, blocked=None, result_status='ready_for_review', blocker_kind=None,
                result_reason=None, result_quote=None, submitted=False, user_submitted=False,
                submission_complete=False, synced=False, closed=False, terminal=False, terminal_closed=False,
                paused=False, errors=[], next_step='', counted=True)
    return {**base, **changes}


def stop(kind=None, status='needs_input', **changes):
    facts = dict(blocked=status, result_status=status, blocker_kind=kind, result_reason='停在表单',
                 result_quote='Please complete the CAPTCHA' if kind == 'captcha' else 'Reference name?')
    return job(**{**facts, **changes})


class StrongModelStopTests(unittest.TestCase):
    def test_strong_model_stop_routes_to_program(self):
        from pipeline.blocker_ledger import classify
        result = classify(stop(status='needs_strong_model'), T0)
        self.assertEqual(result[:2], ('needs_strong_model', 'program'))


class LedgerTests(unittest.TestCase):
    """Migration baseline only: these historical behaviors are not the v2 contract."""
    def setUp(self):
        # classify is shared with v2; freeze the legacy user-routing policy only
        # in this migration baseline, never in active monitor tests.
        from unittest.mock import patch
        from pipeline.blocker_ledger import classify, USER_KINDS
        def legacy_classify(*args, **kwargs):
            result = classify(*args, **kwargs)
            if result and result[0] in USER_KINDS:
                return result[0], 'user', result[2]
            return result
        patcher = patch('pipeline.blocker_ledger.classify', side_effect=legacy_classify)
        patcher.start(); self.addCleanup(patcher.stop)
        self.ledger = LegacyBlockerLedger(':memory:')

    def test_captcha_is_parked_for_the_user_and_reported_once(self):
        jobs = [stop('captcha')]
        new = self.ledger.observe(jobs, [], T0)
        self.assertEqual([(i['job_id'], i['kind']) for i in new], [(1, 'captcha')])
        self.assertIn('CAPTCHA', new[0]['evidence'])
        self.assertEqual(new[0]['thread'], 'codex://threads/owner-1')
        self.assertIsNone(self.ledger.next(T0))
        self.assertEqual(self.ledger.observe(jobs, [], at(1)), [])
        self.assertEqual([i['kind'] for i in self.ledger.parked()], ['captcha'])

    def test_missing_receipt_is_queued_once(self):
        jobs = [job(synced=True, errors=['missing_submission_evidence'])]
        self.ledger.observe(jobs, [], T0)
        self.ledger.observe(jobs, [], at(1))
        todo = self.ledger.next(at(1))
        self.assertEqual((todo['job_id'], todo['kind']), (1, 'source_error'))
        self.assertIn('missing_submission_evidence', todo['evidence'])
        self.assertIsNone(self.ledger.next(at(1)))

    def test_missing_fact_goes_to_controller_then_user_when_profile_lacks_it(self):
        jobs = [stop('missing_fact')]
        self.assertEqual(self.ledger.observe(jobs, [], T0), [])
        todo = self.ledger.next(T0)
        self.assertEqual(todo['kind'], 'missing_fact')
        self.assertEqual(self.ledger.observe(jobs, [], at(1)), [])
        self.ledger.done(todo['id'], 'profile 里确实没有推荐人', at(2), needs_user=True)
        new = self.ledger.observe(jobs, [], at(3))
        self.assertEqual([i['kind'] for i in new], ['missing_fact'])
        self.assertEqual(self.ledger.observe(jobs, [], at(4)), [])
        self.assertEqual([i['job_id'] for i in self.ledger.parked()], [1])
        self.assertIsNone(self.ledger.next(at(4)))

    def test_todos_are_claimed_first_in_first_out(self):
        self.ledger.observe([stop('missing_fact')], [], T0)
        self.ledger.observe([stop('missing_fact'), stop('missing_fact', job_id=2, owner='owner-2')], [], at(1))
        self.assertEqual(self.ledger.next(at(1))['job_id'], 1)
        self.assertEqual(self.ledger.next(at(1))['job_id'], 2)
        self.assertIsNone(self.ledger.next(at(1)))

    def test_result_wait_counts_only_after_the_turn_ends_or_goes_quiet(self):
        self.ledger.observe([stop('missing_fact')], [], T0)
        todo = self.ledger.next(T0)
        self.ledger.done(todo['id'], '让原 Agent 重试', at(1))
        # The agent is working on it: no timer while its turn is active with recent activity.
        running = stop('missing_fact', blocked=None, turn_state='active', turn_at=at(1), last_tool_at=at(1))
        self.ledger.observe([running], [], at(2))
        self.assertIsNone(self.ledger.next(at(2)))
        self.ledger.observe([dict(running, last_tool_at=at(9, 30))], [], at(10))
        self.assertIsNone(self.ledger.next(at(10)))
        # The turn ends with the same blocker: three minutes later it is queued again.
        ended = stop('missing_fact', turn_at=at(11), last_tool_at=at(10))
        self.ledger.observe([ended], [], at(11))
        self.assertIsNone(self.ledger.next(at(11)))
        self.ledger.observe([ended], [], at(13))
        self.assertIsNone(self.ledger.next(at(13)))
        self.ledger.observe([ended], [], at(14))
        again = self.ledger.next(at(14))
        self.assertEqual(again['kind'], 'missing_fact')
        self.assertEqual([h['summary'] for h in again['history']], ['让原 Agent 重试'])
        self.assertEqual(again['attempt'], 2)

    def test_third_todo_demands_a_different_approach(self):
        jobs = [stop(None, status='needs_verification')]
        minute = 0
        texts = []
        for _ in range(3):
            self.ledger.observe([dict(jobs[0], turn_at=at(minute))], [], at(minute))
            todo = self.ledger.next(at(minute))
            texts.append(todo['instruction'])
            self.ledger.done(todo['id'], f'第 {todo["attempt"]} 次处理', at(minute + 1))
            minute += 10
            self.ledger.observe([dict(jobs[0], turn_at=at(minute - 5))], [], at(minute))
        self.assertNotIn('换处理方式', texts[1])
        self.assertIn('换处理方式', texts[2])
        self.assertIn('升级 Sol', texts[2])
        self.assertIn('不要转给用户', texts[2])

    def test_blocker_gone_closes_and_reappearing_reopens_the_same_record(self):
        self.ledger.observe([stop('missing_fact')], [], T0)
        first = self.ledger.next(T0)
        self.ledger.observe([job(result_status='review_passed')], [], at(1))
        self.assertEqual(self.ledger.board()['queue'], [])
        self.assertEqual(self.ledger.board()['waiting'], [])
        self.ledger.observe([stop('missing_fact', turn_at=at(5))], [], at(5))
        again = self.ledger.next(at(5))
        self.assertEqual(again['issue_id'], first['issue_id'])
        self.assertNotEqual(again['id'], first['id'])

    def test_unclaimed_todo_is_dropped_when_the_blocker_disappears(self):
        self.ledger.observe([stop('missing_fact')], [], T0)
        self.ledger.observe([job(result_status='review_passed')], [], at(1))
        self.assertIsNone(self.ledger.next(at(1)))

    def test_sync_waits_fifteen_minutes_but_never_schedules_tab_closure(self):
        ledger = LegacyBlockerLedger(':memory:')
        ledger.observe([job(submitted=True)], [], T0)
        ledger.observe([job(submitted=True)], [], at(14))
        self.assertIsNone(ledger.next(at(14)))
        ledger.observe([job(submitted=True)], [], at(16))
        self.assertEqual(ledger.next(at(16))['kind'], 'sync')
        for facts in (dict(submitted=True, synced=True), dict(terminal=True)):
            with self.subTest(facts=facts):
                ledger = LegacyBlockerLedger(':memory:')
                ledger.observe([job(**facts)], [], T0)
                ledger.observe([job(**facts)], [], at(16))
                self.assertIsNone(ledger.next(at(16)))

    def test_quiet_active_turn_is_hung_after_twenty_minutes(self):
        jobs = [job(turn_state='active', turn_at=T0, last_tool_at=T0)]
        self.ledger.observe(jobs, [], at(19))
        self.assertIsNone(self.ledger.next(at(19)))
        self.ledger.observe(jobs, [], at(21))
        self.assertEqual(self.ledger.next(at(21))['kind'], 'hung')

    def test_global_error_is_a_system_record_without_a_job(self):
        self.ledger.observe([], ['jobfeed_readback:OSError'], T0)
        todo = self.ledger.next(T0)
        self.assertIsNone(todo['job_id'])
        self.assertEqual(todo['kind'], 'system')
        self.assertIn('jobfeed_readback', todo['evidence'])
        self.ledger.observe([], ['jobfeed_readback:OSError'], at(1))
        self.ledger.observe([], [], at(2))
        self.assertEqual(self.ledger.board()['queue'], [])

    def test_submission_unknown_and_ended_without_receipt_go_to_controller(self):
        self.ledger.observe([job(blocked='submission_unknown', result_status='submission_unknown'),
                             job(job_id=2, owner='owner-2', result_status=None)], [], T0)
        self.assertEqual(self.ledger.next(T0)['kind'], 'submission_unknown')
        self.assertEqual(self.ledger.next(T0)['kind'], 'ended_without_receipt')

    def test_closed_terminal_paused_jobs_have_no_blocker(self):
        self.ledger.observe([job(submitted=True, submission_complete=True, synced=True, closed=True),
                             job(job_id=2, terminal=True, terminal_closed=True),
                             job(job_id=3, paused=True, blocked='needs_input', result_status='needs_input'),
                             job(job_id=4, owner=None, turn_state='not_started')], [], at(20))
        self.assertEqual(self.ledger.board(), dict(parked=[], queue=[], waiting=[]))

    def test_done_requires_an_outstanding_todo(self):
        with self.assertRaises(ValueError):
            self.ledger.done(99, 'nothing', T0)

    def test_records_survive_reopening_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'blockers.sqlite3'
            LegacyBlockerLedger(path).observe([stop('missing_fact')], [], T0)
            todo = LegacyBlockerLedger(path).next(at(1))
            self.assertEqual(todo['kind'], 'missing_fact')
            self.assertIsNone(LegacyBlockerLedger(path).next(at(1)))


def at13(minutes):
    """Minutes after the monitor fixture's own clock (its rollout events are stamped there)."""
    return f'2026-10-13T{18 + minutes // 60:02d}:{minutes % 60:02d}:00+00:00'


if __name__ == '__main__':
    unittest.main()
