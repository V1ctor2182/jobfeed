import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pipeline import confirmation_mail
from pipeline.applier_runtime import ApplierRuntime

NEWER, OLDER = 'msg-newer', 'msg-older'


def raw(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


class FakeGmail:
    """A mailbox with two messages from the company; only the older one is a real confirmation."""

    def __init__(self):
        self.calls = []

    def __call__(self, tool, tool_input):
        self.calls.append((tool, tool_input))
        if tool == confirmation_mail.SEARCH:
            return {'threads': [{'messages': [{'id': OLDER, 'internalDate': '1', 'viewUrl': 'https://mail.test/older'},
                                              {'id': NEWER, 'internalDate': '2', 'viewUrl': 'https://mail.test/newer'}]}]}
        return {'raw': raw(f'Message-ID: <{tool_input["messageId"]}@mail.test>\n\nbody')}


class ConfirmationMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ApplierRuntime(Path(self.temp.name))
        self.store.bind(1, 'owner-1', 'batch', 'opus')
        with self.store._write() as db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT, job_id INTEGER, state TEXT)')
            db.execute("INSERT INTO applier_launches VALUES('l1', 1, 'running')")
        self.pipeline = SimpleNamespace(jobfeed=SimpleNamespace(get_job=lambda job_id: SimpleNamespace(company='Acme')))
        self.recorded = []

    def record(self, accepted):
        def record(pipeline, job_id, data, *, source_url, capture_tool):
            message_id = data.decode().split('<')[1].split('>')[0]
            self.recorded.append((job_id, message_id, source_url, capture_tool))
            if message_id.startswith(accepted or '-'):
                return {'status': 'confirmed', 'message_id': f'<{message_id}>'}
            return {'status': 'rejected'}
        return record

    def blocker(self):
        return self.store.ask(1, 'owner-1', 'blocker', 'Clicked Submit once; the page showed nothing.', blocker_type='submit_unknown')

    def test_the_employer_email_answers_submitted_from_source_email_and_the_newest_mail_is_tried_first(self):
        request = self.blocker()
        gmail = FakeGmail()
        events = confirmation_mail.check(self.pipeline, self.store, call=gmail, record=self.record(OLDER))
        self.assertEqual([r[1] for r in self.recorded], [f'{NEWER}@mail.test', f'{OLDER}@mail.test'])
        self.assertEqual(self.recorded[1][2:], ('https://mail.test/older', confirmation_mail.CAPTURE_TOOL))
        self.assertIn('"Acme"', gmail.calls[0][1]['query'])
        reply = self.store.latest_reply(request)
        self.assertEqual((reply['text'], reply['source_kind']), ('submitted', 'email'))
        self.assertEqual(events, [{'event': 'submit_confirmed_by_email', 'job_id': 1, 'message_id': f'<{OLDER}@mail.test>'}])
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=gmail, record=self.record(OLDER)), [], 'an answered blocker is not checked again')

    def test_a_message_the_courier_cannot_return_is_skipped_and_the_next_one_is_tried(self):
        request = self.blocker()

        class Unreadable(FakeGmail):
            def __call__(self, tool, tool_input):
                if tool == confirmation_mail.MESSAGE and tool_input['messageId'] == NEWER:
                    raise confirmation_mail.GmailError('the Gmail result was not JSON')
                return super().__call__(tool, tool_input)

        confirmation_mail.check(self.pipeline, self.store, call=Unreadable(), record=self.record(OLDER))
        self.assertEqual(self.store.latest_reply(request)['source_kind'], 'email')

    def lost_after_submit(self):
        self.store.authorize_submit(1, source_kind='test', event_id='approval-1')
        self.assertTrue(self.store.request_submit(1, 'owner-1')[0])
        return self.store.ask(1, 'owner-1', 'blocker', 'My form tab is gone after the click.', blocker_type='tab_missing')

    def test_a_tab_lost_after_submit_with_no_email_is_marked_for_a_new_run_and_left_unanswered(self):
        # 2026-11-18: the user's standing rule is that such a job is applied again, not asked about.
        request = self.lost_after_submit()
        later = datetime.now(timezone.utc) + confirmation_mail.SETTLE_AFTER + timedelta(minutes=1)
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None)), [], 'it waits for mail first')
        self.assertIsNone(self.store.control(confirmation_mail.no_email_key(request)))
        events = confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None), now=later)
        self.assertEqual(events, [{'event': 'lost_tab_unconfirmed_no_email', 'job_id': 1}])
        self.assertIsNotNone(self.store.control(confirmation_mail.no_email_key(request)))
        self.assertIsNone(self.store.latest_reply(request), 'its form is gone, so it gets no "not submitted"')
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None), now=later), [], 'marked once')

    def test_a_tab_lost_after_submit_is_answered_submitted_by_the_employer_email(self):
        request = self.lost_after_submit()
        confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(OLDER))
        self.assertEqual(self.store.latest_reply(request)['source_kind'], 'email')

    def test_a_tab_lost_before_any_submit_is_not_the_mail_checks(self):
        request = self.store.ask(1, 'owner-1', 'blocker', 'My form tab is gone.', blocker_type='tab_missing')
        later = datetime.now(timezone.utc) + confirmation_mail.SETTLE_AFTER + timedelta(minutes=1)
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None), now=later), [])
        self.assertIsNone(self.store.control(confirmation_mail.no_email_key(request)))

    def test_one_email_confirms_two_blockers_of_the_same_job(self):
        # 2026-11-18: the second reply reused the first one's source ID, raised, and ended the whole pass.
        first = self.lost_after_submit()
        second = self.store.ask(1, 'owner-1', 'blocker', 'My form tab is gone again.', blocker_type='tab_missing')
        events = confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(OLDER))
        self.assertEqual(len(events), 2)
        self.assertEqual([self.store.latest_reply(r)['source_kind'] for r in (first, second)], ['email', 'email'])

    def test_a_blocker_of_an_ended_applier_is_checked_too(self):
        request = self.blocker()
        with self.store._write() as db:
            db.execute("UPDATE applier_launches SET state='finished'")
        confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(OLDER))
        self.assertEqual(self.store.latest_reply(request)['source_kind'], 'email')

    def test_without_an_accepted_email_nothing_is_answered(self):
        request = self.blocker()
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None)), [])
        self.assertIsNone(self.store.latest_reply(request))
        self.assertEqual(self.store.request(request)['state'], 'open')

    def test_mail_already_tried_is_not_fetched_again_and_an_ended_applier_is_not_checked(self):
        self.blocker()
        gmail, tried = FakeGmail(), set()
        confirmation_mail.check(self.pipeline, self.store, call=gmail, record=self.record(None), tried=tried)
        confirmation_mail.check(self.pipeline, self.store, call=gmail, record=self.record(None), tried=tried)
        self.assertEqual(sum(tool == confirmation_mail.MESSAGE for tool, _ in gmail.calls), 2)
        with self.store._write() as db:
            db.execute("UPDATE applier_launches SET state='stopped'")
        gmail.calls.clear()
        confirmation_mail.check(self.pipeline, self.store, call=gmail, record=self.record(OLDER))
        self.assertEqual(gmail.calls, [])

    def test_no_email_within_the_window_answers_not_submitted_without_asking(self):
        request = self.blocker()
        created = datetime.fromisoformat(self.store.request(request)['created_at'])
        early = confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None), now=created + timedelta(minutes=30))
        self.assertEqual((early, self.store.request(request)['state']), ([], 'open'))
        late = confirmation_mail.check(self.pipeline, self.store, call=FakeGmail(), record=self.record(None), now=created + confirmation_mail.SETTLE_AFTER)
        self.assertEqual(late, [{'event': 'submit_unconfirmed_no_email', 'job_id': 1}])
        reply = self.store.latest_reply(request)
        self.assertEqual((reply['text'], reply['source_kind']), ('not submitted', confirmation_mail.NO_EMAIL))

    def test_a_mailbox_that_cannot_be_read_never_answers_not_submitted(self):
        request = self.blocker()
        created = datetime.fromisoformat(self.store.request(request)['created_at'])

        def down(tool, tool_input):
            raise confirmation_mail.GmailError('down')
        self.assertEqual(confirmation_mail.check(self.pipeline, self.store, call=down, record=self.record(None), now=created + timedelta(days=2)), [])
        self.assertEqual(self.store.request(request)['state'], 'open')

    def test_the_user_is_asked_only_once_no_email_came_in_time(self):
        request = self.store.request(self.blocker())
        created = datetime.fromisoformat(request['created_at'])
        self.assertFalse(confirmation_mail.ask_user_now(request, created + timedelta(hours=1)))
        self.assertTrue(confirmation_mail.ask_user_now(request, created + confirmation_mail.ASK_AFTER))


class BridgeHoldTests(unittest.TestCase):
    def test_a_submit_question_reaches_discord_only_after_the_email_window(self):
        from test_applier_discord import FakeDiscord
        from pipeline.applier_discord import DiscordBridge
        with tempfile.TemporaryDirectory() as temp:
            store = ApplierRuntime(Path(temp))
            store.bind(1, 'owner-1', 'batch', 'opus')
            discord = FakeDiscord()
            bridge = DiscordBridge(Path(temp), http=discord, channel_id='general', allowed_user_ids=['400'], job_urls=lambda job_id: '')
            request = store.ask(1, 'owner-1', 'blocker', 'Clicked Submit once; the page showed nothing.', blocker_type='submit_unknown')
            bridge.sync()
            self.assertEqual(discord.messages.get('general', []), [])
            late = (datetime.now(timezone.utc) - confirmation_mail.ASK_AFTER - timedelta(minutes=1)).isoformat()
            with store._write() as db:
                db.execute('UPDATE applier_requests SET created_at=? WHERE id=?', (late, request))
            bridge.sync()
            self.assertEqual(len([m for m in discord.messages['general'] if m['embeds']]), 1)


class EmailReplyInstructionTests(unittest.TestCase):
    def test_an_applier_told_submitted_by_email_records_the_outcome_against_the_saved_email(self):
        import threading
        from pipeline.applier_mcp import EMAIL_CONFIRMED_INSTRUCTION, ApplierTools
        with tempfile.TemporaryDirectory() as temp:
            store = ApplierRuntime(Path(temp))
            store.bind(1, 'owner-1', 'batch', 'opus')
            tools = ApplierTools(store, 1, 'owner-1')
            result = {}
            waiter = threading.Thread(target=lambda: result.update(tools.report_blocker('submit_unknown', 'Clicked Submit once; nothing showed.')))
            waiter.start()
            for _ in range(200):
                opened = [r for r in store.requests() if r['state'] == 'open']
                if opened:
                    break
                threading.Event().wait(0.02)
            store.reply(opened[0]['id'], 'submitted', source_kind='email', source_id='email-1', sender_id='employer-confirmation-email')
            waiter.join(10)
            self.assertEqual((result['reply'], result['next']), ('submitted', EMAIL_CONFIRMED_INSTRUCTION))


if __name__ == '__main__':
    unittest.main()
