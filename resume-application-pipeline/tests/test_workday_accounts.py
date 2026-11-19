import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from pipeline import workday_accounts
from pipeline.applier_discord import review_messages
from pipeline.applier_mcp import ApplierTools
from pipeline.applier_runtime import ApplierRuntime
from pipeline.workday_accounts import (PASSWORD_FILE, AccountStore, PasswordError, WorkdaySite, account_step, execute, find_verification_link, read_password,
                                       save_password, set_password, verification_link, workday_site)

ACME = WorkdaySite('acme.wd5.myworkdayjobs.com', 'acme')
FORM = 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NYC/Engineer_R1/apply/applyManually'
LINK = 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/activate/abc123?redirect=%2Fen-US%2FCareers'
PASSWORD = 'Shared-Pass9'


class FakeBroker:
    """Answers workday_account calls in order, and records every call."""

    def __init__(self, *answers, opened='verified'):
        self.answers, self.opened, self.calls = list(answers), opened, []

    def workday_account(self, job_id, page_url, email, mode):
        self.calls.append(('workday_account', page_url, email, mode))
        return self.answers.pop(0)

    def open_link(self, url, expected_site):
        self.calls.append(('open_link', url, expected_site))
        return {'outcome': self.opened}


def no_sleep(_seconds):
    pass


class WorkdaySiteTests(unittest.TestCase):
    def test_a_tenant_host_and_a_shared_host_path_are_each_one_site(self):
        self.assertEqual(workday_site(FORM), ACME)
        self.assertEqual(workday_site('https://wd1.myworkdaysite.com/en-US/recruiting/globex/External/job/1'),
                         WorkdaySite('wd1.myworkdaysite.com/recruiting/globex', 'globex'))
        self.assertEqual(ACME.senders, ('acme@myworkday.com', 'acme@otp.workday.com'))

    def test_other_pages_are_not_workday(self):
        for url in ('http://acme.wd5.myworkdayjobs.com/x', 'https://acme.wd5.myworkdayjobs.com.evil.test/x', 'https://evil.test/acme.wd5.myworkdayjobs.com',
                    'https://user:pw@acme.wd5.myworkdayjobs.com/x', 'https://boards.greenhouse.io/acme', 'https://wd1.myworkdaysite.com/en-US/External',
                    'not a url', None):
            self.assertIsNone(workday_site(url), url)


class VerificationLinkTests(unittest.TestCase):
    def test_the_one_link_on_the_account_site_is_accepted(self):
        body = f'Welcome! Verify your account: {LINK}\nOr open the careers page https://acme.wd5.myworkdayjobs.com/en-US/Careers'
        self.assertEqual(verification_link(body, ACME), LINK)
        self.assertEqual(verification_link(f'[Verify]({LINK}) or {LINK}.', ACME), LINK)

    def test_links_to_any_other_site_are_refused(self):
        for link in ('https://evil.test/activate/abc', 'https://acme.wd5.myworkdayjobs.com.evil.test/activate/abc',
                     'https://globex.wd5.myworkdayjobs.com/en-US/Careers/activate/abc', 'http://acme.wd5.myworkdayjobs.com/activate/abc'):
            self.assertIsNone(verification_link(f'Verify your account: {link}', ACME), link)
        shared = WorkdaySite('wd1.myworkdaysite.com/recruiting/acme', 'acme')
        self.assertIsNone(verification_link('https://wd1.myworkdaysite.com/recruiting/globex/External/activate/abc', shared))

    def test_two_candidate_links_are_refused(self):
        self.assertIsNone(verification_link(f'{LINK} https://acme.wd5.myworkdayjobs.com/en-US/Careers/activate/other', ACME))

    def test_gmail_is_read_from_the_tenant_sender_only_and_newer_than_since(self):
        calls = []

        def call(tool, arguments):
            calls.append((tool, arguments))
            if tool.endswith('search_threads'):
                return {'threads': [{'id': 't1', 'messages': [
                    {'id': 'old', 'threadId': 't1', 'sender': 'acme@myworkday.com', 'internalDate': '1000'},
                    {'id': 'spoof', 'threadId': 't1', 'sender': 'acme@myworkday.com.evil.test', 'internalDate': '9000000'},
                    {'id': 'spoof2', 'threadId': 't1', 'sender': 'acme@otp.workday.com.evil.test', 'internalDate': '9000000'},
                    {'id': 'new', 'threadId': 't1', 'sender': 'acme@otp.workday.com', 'internalDate': '5000000'}]}]}
            return {'messages': [{'id': 'old', 'plaintextBody': 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/activate/stale'},
                                 {'id': 'new', 'plaintextBody': f'Verify: {LINK}'}]}

        self.assertEqual(find_verification_link(ACME, 'me@example.test', 4000, call), LINK)
        self.assertIn('from:acme@myworkday.com OR from:acme@otp.workday.com', calls[0][1]['query'])
        self.assertEqual([c[1].get('threadId') for c in calls[1:]], ['t1'])


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_set_password_asks_twice_and_saves_it_private(self):
        answers = iter([PASSWORD, PASSWORD])
        set_password(self.root, lambda _prompt: next(answers))
        path = self.root / '.pipeline/account-password'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(read_password(self.root), PASSWORD)

    def test_a_mismatch_or_a_weak_password_saves_nothing(self):
        answers = iter([PASSWORD, PASSWORD + 'x'])
        with self.assertRaisesRegex(PasswordError, 'differ'):
            set_password(self.root, lambda _prompt: next(answers))
        for weak in ('Short1!', 'nouppercase1!', 'NOLOWERCASE1!', 'NoDigits!!', 'NoSpecial12', 'Has space1!'):
            with self.assertRaises(PasswordError, msg=weak):
                set_password(self.root, lambda _prompt, weak=weak: weak)
        self.assertIsNone(read_password(self.root))

    def test_a_password_file_others_can_read_is_refused(self):
        save_password(self.root, PASSWORD)
        os.chmod(self.root / '.pipeline/account-password', 0o644)
        with self.assertRaises(PasswordError):
            read_password(self.root)

    def test_save_password_reads_standard_input_once_and_never_shows_it(self):
        import io
        shown = execute(self.root, argparse.Namespace(accounts_command='save-password'), stdin=io.StringIO('Tr1cky-pass!\n'))
        self.assertEqual(shown, {'saved': True, 'password_set': True, 'accounts': 0})
        self.assertEqual(read_password(self.root), 'Tr1cky-pass!')
        self.assertEqual((self.root / PASSWORD_FILE).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(PasswordError, 'at least 8 characters') as refused:
            execute(self.root, argparse.Namespace(accounts_command='save-password'), stdin=io.StringIO('short'))
        self.assertNotIn('short', str(refused.exception))
        self.assertEqual(read_password(self.root), 'Tr1cky-pass!', 'a refused password leaves the saved one')

    def test_the_cli_never_shows_the_password(self):
        answers = iter([PASSWORD, PASSWORD])
        shown = [execute(self.root, argparse.Namespace(accounts_command='set-password'), lambda _prompt: next(answers))]
        AccountStore(self.root).record(ACME, email='me@example.test', state='signed_in', job_id=7, created=True)
        shown.append(execute(self.root, argparse.Namespace(accounts_command='list')))
        self.assertEqual([a['site'] for a in shown[1]['accounts']], [ACME.key])
        self.assertTrue(shown[1]['password_set'])
        self.assertNotIn(PASSWORD, json.dumps(shown))
        self.assertEqual(execute(self.root, argparse.Namespace(accounts_command='forget', site=ACME.key))['forgotten'], True)
        self.assertEqual(AccountStore(self.root).accounts(), [])


class AccountStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(self.temp.name)

    def step(self, broker, find_link=lambda *_: None):
        return account_step(broker, self.store, ACME, 'me@example.test', 7, FORM, find_link, polls=3, wait=0, sleep=no_sleep)

    def test_a_new_account_is_created_verified_and_signed_in(self):
        broker = FakeBroker({'outcome': 'verification_sent'}, {'outcome': 'signed_in'})
        result = self.step(broker, lambda site, email, since: LINK if site == ACME and email == 'me@example.test' else None)
        self.assertEqual(result['outcome'], 'signed_in')
        self.assertIn('continue filling', result['next'])
        self.assertEqual(broker.calls, [('workday_account', FORM, 'me@example.test', 'create'), ('open_link', LINK, ACME.key),
                                        ('workday_account', FORM, 'me@example.test', 'sign_in')])
        self.assertEqual({k: self.store.get(ACME.key)[k] for k in ('state', 'created_by_job', 'email')},
                         {'state': 'signed_in', 'created_by_job': 7, 'email': 'me@example.test'})
        self.assertEqual(self.store.created_by(7), [{'site': ACME.key, 'email': 'me@example.test'}])

    def test_a_known_account_signs_in_without_creating(self):
        self.store.record(ACME, email='me@example.test', state='signed_in', job_id=3, created=True)
        broker = FakeBroker({'outcome': 'signed_in'})
        self.assertEqual(self.step(broker)['outcome'], 'signed_in')
        self.assertEqual(broker.calls[0][3], 'sign_in')
        self.assertEqual(self.store.created_by(7), [])
        self.assertEqual(self.store.created_by(3), [{'site': ACME.key, 'email': 'me@example.test'}])

    def test_an_email_in_use_tries_sign_in_and_a_wrong_password_goes_to_the_user(self):
        broker = FakeBroker({'outcome': 'email_in_use'}, {'outcome': 'wrong_password'})
        result = self.step(broker)
        self.assertEqual(result['outcome'], 'wrong_password')
        self.assertEqual([c[3] for c in broker.calls], ['create', 'sign_in'])
        self.assertIn('blocker_type user_only_check', result['next'])
        self.assertEqual(self.store.get(ACME.key)['state'], 'failed')
        self.assertEqual(self.store.created_by(7), [])

    def test_no_verification_email_asks_the_user_and_opens_nothing(self):
        broker = FakeBroker({'outcome': 'verification_sent'})
        result = self.step(broker)
        self.assertEqual(result['outcome'], 'needs_verification')
        self.assertNotIn('open_link', [c[0] for c in broker.calls])
        self.assertIn('acme@myworkday.com', result['next'])
        self.assertEqual(self.store.get(ACME.key)['state'], 'created')

    def test_an_account_still_unverified_after_the_link_asks_the_user(self):
        broker = FakeBroker({'outcome': 'verification_sent'}, {'outcome': 'verification_sent'}, opened='unknown_page')
        self.assertEqual(self.step(broker, lambda *_: LINK)['outcome'], 'needs_verification')

    def test_a_captcha_reports_the_existing_captcha_blocker(self):
        result = self.step(FakeBroker({'outcome': 'captcha'}))
        self.assertIn('blocker_type captcha', result['next'])
        self.assertIsNone(self.store.get(ACME.key))


class ApplierAccountStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'application-profile.md').write_text('# Profile\n\n## Identity and contact\n\n- Primary email: me@example.test\n')
        self.runtime = ApplierRuntime(self.root)
        self.runtime.bind(1, 'owner-1', 'batch')

    def tools(self, broker):
        return ApplierTools(self.runtime, 1, 'owner-1', broker=broker, find_link=lambda *_: None)

    def test_it_signs_in_with_the_profile_email_on_the_registered_site(self):
        self.runtime.register_tab(1, 'owner-1', 5, 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NYC/Engineer_R1')
        broker = FakeBroker({'outcome': 'signed_in'})
        result = self.tools(broker).account_step(FORM)
        self.assertEqual((result['outcome'], result['email']), ('signed_in', 'me@example.test'))
        self.assertEqual(broker.calls, [('workday_account', FORM, 'me@example.test', 'create')])
        self.assertEqual(AccountStore(self.root).created_by(1), [{'site': ACME.key, 'email': 'me@example.test'}])

    def test_it_refuses_pages_off_the_registered_workday_site(self):
        broker = FakeBroker()
        with self.assertRaisesRegex(ValueError, 'register_tab'):
            self.tools(broker).account_step(FORM)
        self.runtime.register_tab(1, 'owner-1', 5, 'https://globex.wd5.myworkdayjobs.com/en-US/Careers/job/1')
        with self.assertRaisesRegex(ValueError, 'same Workday site'):
            self.tools(broker).account_step(FORM)
        with self.assertRaisesRegex(ValueError, 'Workday pages'):
            self.tools(broker).account_step('https://boards.greenhouse.io/acme/jobs/1')
        self.assertEqual(broker.calls, [])

    def test_an_unreachable_browser_alerts_the_user_and_reports_a_blocker(self):
        self.runtime.register_tab(1, 'owner-1', 5, FORM)
        result = self.tools(FakeBroker({'outcome': 'not_connected'})).account_step(FORM)
        self.assertEqual(result['outcome'], 'not_connected')
        self.assertIn('blocker_type user_only_check', result['next'])
        self.assertEqual([(a['job_id'], a['kind']) for a in self.runtime.alerts(unsent=True)], [(1, 'account_program')])
        self.assertIn('jobfeed-apply-chromium', self.runtime.alerts()[0]['detail'])


class ReviewSummaryTests(unittest.TestCase):
    def test_the_summary_names_the_account_the_job_created(self):
        message = review_messages(1, '', [('Name', 'V')], accounts=[{'site': ACME.key, 'email': 'me@example.test'}])[0]
        description = message['embeds'][0]['description']
        self.assertIn(ACME.key, description)
        self.assertIn('me@example.test', description)
        self.assertNotIn('Account', review_messages(1, '', [('Name', 'V')])[0]['embeds'][0]['description'])


if __name__ == '__main__':
    unittest.main()
