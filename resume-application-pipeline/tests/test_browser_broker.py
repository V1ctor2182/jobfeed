from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from pipeline import browser_broker
from pipeline.browser_broker import Broker, BrokerClient, Chromium, account_on_page, classify, serve
from pipeline.workday_accounts import save_password

FORM = 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NYC/Engineer_R1/apply/applyManually'
PASSWORD = 'Shared-Pass9'


def snapshot(**fields):
    return dict({'url': FORM, 'ready': True, 'captcha': False, 'text': 'Create Account', 'errors': [], 'password': True,
                 'create_form': True, 'sign_in_form': False, 'create_link': False, 'sign_in_link': True}, **fields)


class FakeCdp:
    def __init__(self, targets):
        self.targets, self.calls = targets, []

    def call(self, method, params=None, session=None, timeout=30.0):
        self.calls.append((method, params, session))
        if method == 'Target.getTargets':
            return {'targetInfos': self.targets}
        if method == 'Target.attachToTarget':
            return {'sessionId': f"session-{params['targetId']}"}
        return {}


class FakeChromium:
    def __init__(self, cdp):
        self.cdp = cdp

    def session(self):
        return self.cdp

    def drop(self):
        pass


class BrokerTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save_password(self.root, PASSWORD)

    def request(self, targets):
        cdp = FakeCdp(targets)
        pages = []

        def on_page(page, email, password, mode):
            pages.append((page.session, email, password, mode))
            return {'outcome': 'signed_in'}

        with patch.object(browser_broker, 'account_on_page', on_page):
            result = Broker(self.root, FakeChromium(cdp)).handle({'type': 'workday_account', 'job_id': 1, 'page_url': FORM, 'email': 'me@example.test',
                                                                   'mode': 'sign_in'})
        return result, pages, cdp

    def test_it_acts_on_the_one_tab_showing_the_page(self):
        result, pages, cdp = self.request([{'targetId': 'a', 'type': 'page', 'url': FORM + '?x=1'}, {'targetId': 'b', 'type': 'page', 'url': FORM},
                                           {'targetId': 'c', 'type': 'service_worker', 'url': FORM}])
        self.assertEqual(result, {'outcome': 'signed_in'})
        self.assertEqual(pages, [('session-b', 'me@example.test', PASSWORD, 'sign_in')])
        self.assertIn(('Target.detachFromTarget', {'sessionId': 'session-b'}, None), cdp.calls)

    def test_no_tab_or_two_tabs_with_the_page_is_an_error(self):
        for targets in ([], [{'targetId': 'a', 'type': 'page', 'url': FORM}, {'targetId': 'b', 'type': 'page', 'url': FORM}]):
            result, pages, _cdp = self.request(targets)
            self.assertEqual(result['outcome'], 'tab_not_found')
            self.assertEqual(pages, [])

    def test_requests_off_workday_or_to_another_site_are_refused(self):
        broker = Broker(self.root, FakeChromium(FakeCdp([])))
        self.assertEqual(broker.handle({'type': 'workday_account', 'page_url': 'https://evil.test/login', 'email': 'me@example.test', 'mode': 'create'})['outcome'],
                         'invalid')
        self.assertEqual(broker.handle({'type': 'open_link', 'url': 'https://globex.wd5.myworkdayjobs.com/activate/x',
                                        'expected_site': 'acme.wd5.myworkdayjobs.com'})['outcome'], 'invalid')

    def test_no_password_set_touches_no_tab(self):
        (self.root / '.pipeline/account-password').unlink()
        cdp = FakeCdp([{'targetId': 'b', 'type': 'page', 'url': FORM}])
        result = Broker(self.root, FakeChromium(cdp)).handle({'type': 'workday_account', 'page_url': FORM, 'email': 'me@example.test', 'mode': 'create'})
        self.assertEqual(result['outcome'], 'no_password')
        self.assertEqual(cdp.calls, [])

    def test_a_browser_that_is_down_answers_not_connected_and_the_next_request_connects_once_it_is_up(self):
        endpoint = 'ws://127.0.0.1:9333/devtools/browser/abc'

        class Version(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == '/json/version' else 404)
                self.end_headers()
                self.wfile.write(json.dumps({'webSocketDebuggerUrl': endpoint}).encode())

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Version) as server:
            port = server.server_address[1]
        urls, cdp = [], FakeCdp([])
        broker = Broker(self.root, Chromium(f'http://127.0.0.1:{port}', connect=lambda url, timeout: urls.append(url), timeout=2))
        request = {'type': 'workday_account', 'page_url': FORM, 'email': 'me@example.test', 'mode': 'create'}
        self.assertEqual(broker.handle(request)['outcome'], 'not_connected')
        with HTTPServer(('127.0.0.1', port), Version) as server, patch.object(browser_broker, 'Cdp', lambda ws: cdp):
            threading.Thread(target=server.handle_request, daemon=True).start()
            self.assertEqual(broker.handle(request)['outcome'], 'tab_not_found')
        self.assertEqual(urls, [endpoint])


class FakePage:
    """A Workday form that answers each submit with the next snapshot."""

    def __init__(self, before, after):
        self.before, self.after, self.submitted, self.typed, self.clicked = before, after, False, [], []

    def snapshot(self):
        return self.after if self.submitted else self.before

    def wait(self, done, timeout):
        return self.snapshot()

    def fill(self, name, value, *, secret=False):
        self.typed.append((name, value, secret))
        return True

    def check(self, name):
        return True

    def click(self, name):
        self.clicked.append(name)
        self.submitted = self.submitted or name.endswith('SubmitButton')
        return True


class AccountOnPageTests(unittest.TestCase):
    def run_page(self, page, mode):
        with patch.object(browser_broker.time, 'sleep'):
            return account_on_page(page, 'me@example.test', PASSWORD, mode, settle=0)

    def test_create_types_the_password_only_as_a_secret_and_reports_the_email_sent(self):
        page = FakePage(snapshot(), snapshot(create_form=False, text='We sent an email. Verify your account to continue.'))
        self.assertEqual(self.run_page(page, 'create'), {'outcome': 'verification_sent'})
        self.assertEqual(page.typed, [('email', 'me@example.test', False), ('password', PASSWORD, True), ('verifyPassword', PASSWORD, True)])
        self.assertEqual(page.clicked, ['createAccountSubmitButton'])

    def test_sign_in_opens_the_sign_in_form_first(self):
        page = FakePage(snapshot(), snapshot(password=False, create_form=False, text='My Information'))
        page.wait = lambda done, timeout: snapshot(create_form=False, sign_in_form=True)
        self.assertEqual(self.run_page(page, 'sign_in'), {'outcome': 'signed_in'})
        self.assertEqual(page.clicked, ['signInLink', 'signInSubmitButton'])

    def test_a_captcha_stops_before_typing(self):
        page = FakePage(snapshot(captcha=True), snapshot())
        self.assertEqual(self.run_page(page, 'create'), {'outcome': 'captcha'})
        self.assertEqual(page.typed, [])


class ClassifyTests(unittest.TestCase):
    def test_page_results(self):
        self.assertEqual(classify(snapshot(errors=['An account with this email already exists.']), 'create')[0], 'email_in_use')
        self.assertEqual(classify(snapshot(create_form=False, sign_in_form=True, errors=['Wrong email address or password.']), 'sign_in')[0],
                         'wrong_password')
        self.assertEqual(classify(snapshot(create_form=False, sign_in_form=True, errors=['Your account has not been verified.']), 'sign_in')[0],
                         'verification_sent')
        self.assertEqual(classify(snapshot(password=False, create_form=False, text='My Information'), 'sign_in')[0], 'signed_in')
        self.assertEqual(classify(snapshot(password=False, create_form=False, text=''), 'sign_in')[0], None)
        self.assertEqual(classify(snapshot(), 'create'), (None, ''))
        self.assertEqual(classify(snapshot(errors=['Password must contain a number']), 'create'), ('unknown_page', 'Password must contain a number'))


class SocketTests(unittest.TestCase):
    def test_a_request_round_trips_on_a_private_socket_even_under_a_long_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ('deep-' * 25)
            root.mkdir()
            self.assertEqual(BrokerClient(root).workday_account(1, FORM, 'me@example.test', 'create'), {'outcome': 'broker_unavailable'})
            seen = []

            class Echo:
                def handle(self, request):
                    seen.append(request)
                    return {'outcome': 'signed_in'}

            ready, stop = threading.Event(), threading.Event()
            server = threading.Thread(target=serve, args=(root, Echo()), kwargs={'ready': ready, 'stop': stop}, daemon=True)
            server.start()
            self.assertTrue(ready.wait(5))
            try:
                self.assertGreater(len(str(root / '.pipeline/browser-broker.sock')), 108)
                self.assertEqual(stat.S_IMODE(os.stat(root / '.pipeline/browser-broker.sock').st_mode), 0o600)
                self.assertEqual(BrokerClient(root).workday_account(1, FORM, 'me@example.test', 'create'), {'outcome': 'signed_in'})
                self.assertEqual(seen, [{'type': 'workday_account', 'job_id': 1, 'page_url': FORM, 'email': 'me@example.test', 'mode': 'create'}])
            finally:
                stop.set()
                server.join(5)


class PasswordPage:
    """A page with some password fields. It answers the three scripts passwords_on_page runs, in order per field."""

    def __init__(self, fields, captcha=False, takes=True):
        self.fields, self.captcha, self.takes, self.typed = fields, captcha, takes, []
        self.session, self.cdp = 'session-b', self

    def snapshot(self):
        return snapshot(captcha=self.captcha)

    def evaluate(self, expression):
        if expression.endswith('.length; })()'):
            return self.fields
        if 'el.focus()' in expression:
            return True
        return self.takes

    def call(self, method, params=None, session=None):
        self.typed.append((method, params['text'], session))
        return {}


class FillPasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save_password(self.root, PASSWORD)

    def test_every_visible_password_field_takes_the_shared_password(self):
        page = PasswordPage(2)
        self.assertEqual(browser_broker.passwords_on_page(page, PASSWORD), {'outcome': 'filled', 'fields': 2})
        self.assertEqual(page.typed, [('Input.insertText', PASSWORD, 'session-b')] * 2)
        self.assertEqual(browser_broker.passwords_on_page(PasswordPage(0), PASSWORD)['outcome'], 'no_password_field')
        self.assertEqual(browser_broker.passwords_on_page(PasswordPage(1, captcha=True), PASSWORD), {'outcome': 'captcha'})
        self.assertEqual(browser_broker.passwords_on_page(PasswordPage(1, takes=False), PASSWORD)['outcome'], 'unknown_page')

    def test_the_shared_password_is_never_typed_on_a_personal_account_page(self):
        for url in ('https://accounts.google.com/signin', 'https://login.live.com/', 'https://idmsa.apple.com/appleauth', 'https://www.linkedin.com/login',
                    'http://careers.acme.test/login', 'https://user:pw@careers.acme.test/login', 'not a url'):
            self.assertIsNone(browser_broker.account_host(url), url)
            cdp = FakeCdp([{'targetId': 'b', 'type': 'page', 'url': url}])
            self.assertEqual(Broker(self.root, FakeChromium(cdp)).handle({'type': 'fill_password', 'page_url': url})['outcome'], 'personal_account')
            self.assertEqual(cdp.calls, [])
        self.assertEqual(browser_broker.account_host('https://career4.successfactors.com/careers?company=acme'), 'career4.successfactors.com')
        self.assertEqual(browser_broker.account_host('https://apply.careers.microsoft.com/careers'), 'apply.careers.microsoft.com')

    def test_it_fills_the_one_tab_showing_the_page(self):
        url = 'https://careers.acme.test/register'
        cdp, seen = FakeCdp([{'targetId': 'b', 'type': 'page', 'url': url}]), []

        def on_page(page, password):
            seen.append((page.session, password))
            return {'outcome': 'filled', 'fields': 2}
        with patch.object(browser_broker, 'passwords_on_page', on_page):
            self.assertEqual(Broker(self.root, FakeChromium(cdp)).handle({'type': 'fill_password', 'job_id': 1, 'page_url': url}), {'outcome': 'filled', 'fields': 2})
            self.assertEqual(Broker(self.root, FakeChromium(FakeCdp([]))).handle({'type': 'fill_password', 'page_url': url})['outcome'], 'tab_not_found')
        self.assertEqual(seen, [('session-b', PASSWORD)])


if __name__ == '__main__':
    unittest.main()
