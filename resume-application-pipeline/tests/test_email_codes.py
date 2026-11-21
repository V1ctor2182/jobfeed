from pathlib import Path
import tempfile
import threading
import unittest

from pipeline import confirmation_mail, email_codes
from pipeline.applier_mcp import CODE_FOUND, CODE_MISSING, NO_EMAIL_INSTRUCTION, ApplierTools
from pipeline.applier_runtime import ApplierRuntime
from pipeline.workday_accounts import GmailError


def mailbox(*messages):
    """A Gmail double: each message is (id, epoch seconds, sender, subject, body)."""
    def call(tool, tool_input):
        if tool == email_codes.SEARCH:
            # Like Gmail since 2026-11-21: a search hit has no time; its thread does.
            return {'threads': [{'messages': [{'id': m[0], 'threadId': 't-' + m[0], 'sender': m[2]}]} for m in messages]}
        m = next(m for m in messages if 't-' + m[0] == tool_input['threadId'])
        return {'messages': [{'id': m[0], 'internalDate': str(m[1] * 1000), 'sender': m[2], 'subject': m[3], 'plaintextBody': m[4]}]}
    return call


class CodeTests(unittest.TestCase):
    def test_only_digits_near_a_code_word_count_and_a_year_is_not_a_code(self):
        self.assertEqual(email_codes.code_in('Your verification code is 482913. It expires in 10 minutes. © 2026 Acme'), '482913')
        self.assertEqual(email_codes.code_in('Order 123456 shipped to 10001'), None)
        oracle = ('We need you to confirm your identity so that your job application is considered for the job Data Engineer II - 5521.\n\n'
                  'Confirm your identity using this code: 730184.\n\nThe code expires in 10 minutes.')
        self.assertEqual(email_codes.code_in(oracle), '730184', 'a job number near the word is not the code')
        self.assertEqual(email_codes.code_in('Use code 111111 or code 222222'), None, 'two different codes are refused')
        self.assertEqual(email_codes.code_in('Call 2125550100 about your one-time passcode'), None, 'a phone number is too long to be a code')

    def test_the_page_host_and_company_tie_an_email_to_the_application(self):
        self.assertEqual(email_codes.names('Uber', 'https://jobs.uber.com/apply'), {'uber', 'uber.com'})
        self.assertEqual(email_codes.names(None, 'https://hdpc.fa.us2.oraclecloud.com/x'), {'hdpc', 'oraclecloud', 'oraclecloud.com'})
        self.assertEqual(email_codes.names(None, 'https://careers.acme.co.uk/x'), {'acme'}, 'a country suffix is not a site')

    def test_a_platform_code_sent_from_the_page_site_counts_without_the_employer_name(self):
        adp = ('Verification code 518169 This code expires in 15 minutes. Enter this code to access ADP services. '
               'The email is sent from an automated system so please do not reply. ca63fc5e-0078-4c46-86e7-9b29c099034b')
        call = mailbox(('adp', 200, 'SecurityServices_NoReply@adp.com', "Here's your verification code from ADP", adp))
        tied_to = email_codes.names('Stellantis', 'https://myjobs.adp.com/stellantisexternalcx/cx/job-details')
        self.assertEqual(email_codes.find_code('me@example.test', 100, tied_to, call), '518169')
        self.assertEqual(email_codes.find_code('me@example.test', 100, email_codes.names('Acme', 'https://jobs.acme.test/x'), call), None)

    def test_a_message_the_courier_cannot_return_does_not_hide_the_code(self):
        good = mailbox(('code', 200, 'Acme <jobs@acme.test>', 'Your code', 'Verification code: 482913'))

        def call(tool, tool_input):
            if tool == email_codes.SEARCH:
                found = good(tool, tool_input)
                found['threads'].insert(0, {'messages': [{'id': 'big', 'threadId': 't-big', 'internalDate': '300000', 'sender': 'x@y.test'}]})
                return found
            if tool_input['threadId'] == 't-big':
                raise GmailError('the Gmail result was not JSON')
            return good(tool, tool_input)
        self.assertEqual(email_codes.find_code('me@example.test', 100, {'acme'}, call), '482913')

    def test_the_newest_email_after_the_request_that_names_the_site_gives_the_code(self):
        call = mailbox(('old', 50, 'Uber <no-reply@uber.com>', 'Your code', 'Verification code: 111111'),
                       ('bank', 300, 'Bank <alerts@bank.test>', 'Security code', 'Your one-time code is 999999'),
                       ('new', 200, 'Uber Careers <no-reply@oraclecloud.com>', 'Confirm your email', 'Uber: your verification code is 654321'))
        self.assertEqual(email_codes.find_code('me@example.test', 100, {'uber'}, call), '654321')
        self.assertEqual(email_codes.find_code('me@example.test', 100, {'acme'}, call), None)

    def test_only_a_confirmation_link_on_the_same_site_is_returned(self):
        body = 'Welcome. Confirm your account: https://career4.successfactors.com/verify?token=abc. Unsubscribe: https://mail.evil.test/verify?x=1'
        self.assertEqual(email_codes.link_in(body, 'successfactors.com'), 'https://career4.successfactors.com/verify?token=abc')
        self.assertEqual(email_codes.link_in(body, 'acme.test'), None)
        self.assertEqual(email_codes.link_in('https://a.acme.test/verify/1 https://b.acme.test/activate/2', 'acme.test'), None, 'two candidates are refused')
        call = mailbox(('m', 200, 'Acme <jobs@acme.test>', 'Activate', 'Activate here: https://jobs.acme.test/activate/9'))
        self.assertEqual(email_codes.find_link('me@example.test', 100, 'acme.test', call), 'https://jobs.acme.test/activate/9')
        self.assertEqual(email_codes.find_link('me@example.test', 300, 'acme.test', call), None)

    def test_waiting_polls_until_a_code_arrives_and_survives_a_mail_error(self):
        answers = iter([GmailError('down'), None, '424242'])

        def find(email, since, tied_to):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.assertEqual(email_codes.wait_for_code('me@example.test', {'acme'}, find=find, sleep=lambda s: None, clock=lambda: 5000), '424242')
        self.assertEqual(email_codes.wait_for_code('me@example.test', {'acme'}, find=lambda *a: None, polls=2, sleep=lambda s: None), None)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ApplierRuntime(Path(self.temp.name))
        self.store.bind(1, 'owner-1', 'batch', 'opus')

    def tools(self, find_code=None):
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme', find_code=find_code)
        tools._profile_email = lambda: 'me@example.test'
        return tools

    def test_email_code_returns_the_code_and_never_records_it(self):
        seen = []
        result = self.tools(lambda email, tied_to: seen.append((email, tied_to)) or '482913').email_code('https://careers.acme.test/verify')
        self.assertEqual((result['code'], result['next']), ('482913', CODE_FOUND))
        self.assertEqual(seen, [('me@example.test', {'acme', 'acme.test'})])
        self.assertNotIn('482913', Path(self.store.path).read_bytes().decode(errors='ignore'))
        self.assertEqual(self.tools(lambda email, tied_to: None).email_code('https://careers.acme.test/verify'), {'code': None, 'next': CODE_MISSING})

    def test_fill_password_asks_the_account_program_and_never_returns_a_password(self):
        from pipeline.applier_mcp import PASSWORD_NEXT

        class Broker:
            def __init__(self, result):
                self.result, self.calls = result, []

            def fill_password(self, job_id, page_url):
                self.calls.append((job_id, page_url))
                return self.result
        tools, url = self.tools(), 'https://careers.acme.test/register'
        with self.assertRaises(ValueError):
            tools.fill_password(url)
        self.store.register_tab(1, 'owner-1', 5, 'https://careers.acme.test/apply')
        tools.broker = Broker({'outcome': 'filled', 'fields': 2})
        result = tools.fill_password(url)
        self.assertEqual((result['outcome'], tools.broker.calls), ('filled', [(1, url)]))
        self.assertIn("2 password field(s)", result['next'])
        tools.broker = Broker({'outcome': 'personal_account'})
        self.assertEqual(tools.fill_password('https://accounts.google.com/x')['next'], PASSWORD_NEXT['personal_account'])
        tools.broker = Broker({'outcome': 'unknown_page', 'reason': 'field 1 did not take the text'})
        self.assertIn('field 1 did not take the text', tools.fill_password(url)['next'])

    def test_email_link_returns_only_what_the_program_found_for_the_page_site(self):
        from pipeline.applier_mcp import LINK_FOUND, LINK_MISSING
        seen = []
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme', find_email_link=lambda email, site: seen.append((email, site)) or 'https://jobs.acme.test/activate/9')
        tools._profile_email = lambda: 'me@example.test'
        self.assertEqual(tools.email_link('https://careers.acme.test/check-email'), {'link': 'https://jobs.acme.test/activate/9', 'next': LINK_FOUND})
        self.assertEqual(seen, [('me@example.test', 'acme.test')])
        tools.find_email_link = lambda email, site: None
        self.assertEqual(tools.email_link('https://careers.acme.test/check-email')['next'], LINK_MISSING)
        with self.assertRaises(ValueError):
            tools.email_link('http://careers.acme.test/check-email')

    def test_a_stated_no_sponsorship_rule_skips_the_job_without_asking(self):
        tools = self.tools()
        with self.assertRaises(ValueError):
            tools.report_no_sponsorship('The team is hybrid in Austin.')
        result = tools.report_no_sponsorship('This position does not offer visa sponsorship now or in the future.')
        self.assertIn('close every tab in your own tab group', result['next'])
        skip = self.store.skip(1)
        self.assertEqual((skip['source_kind'], skip['sender_id']), ('posting', 'applier'))
        self.assertTrue(skip['reason'].startswith('No sponsorship: This position does not offer'))
        self.assertEqual([row['kind'] for row in self.store.outbox()], ['user_skip'])

    def test_no_confirmation_email_tells_the_applier_it_was_not_submitted(self):
        tools, result = self.tools(), {}
        waiter = threading.Thread(target=lambda: result.update(tools.report_blocker('submit_unknown', 'Clicked Submit once; nothing showed.')))
        waiter.start()
        for _ in range(200):
            opened = [r for r in self.store.requests() if r['state'] == 'open']
            if opened:
                break
            threading.Event().wait(0.02)
        self.store.reply(opened[0]['id'], 'not submitted', source_kind=confirmation_mail.NO_EMAIL, source_id='no-email-1', sender_id='no-confirmation-email')
        waiter.join(10)
        self.assertEqual(result['reply'], 'not submitted')
        self.assertTrue(result['next'].startswith(NO_EMAIL_INSTRUCTION))


if __name__ == '__main__':
    unittest.main()
