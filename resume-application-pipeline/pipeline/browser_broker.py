"""The account program: one long-running process that drives the applying browser over DevTools.

The applying browser is a Chromium profile of its own with a fixed DevTools port, so connecting needs no Allow click.
Appliers ask this program, over a unix socket under .pipeline/, to sign in or create a Workday account on their own
tab, or to open a verification link. It types the shared password itself; no reply, log line or error carries it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.parse import urlsplit
import urllib.request

from pipeline.workday_accounts import PasswordError, read_password, workday_site

SOCKET = 'browser-broker.sock'
BROWSER_URL = 'http://127.0.0.1:9333'
MODES = frozenset({'create', 'sign_in'})
MAX_REQUEST = 65536
_EMAIL = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')
VERIFY_PHRASES = ('verify your account', 'verification email', 'verify your email', 'activate your account', 'not been verified',
                  'not yet verified', 'check your email', 'sent you an email', 'sent an email')
VERIFIED_WORDS = ('verified', 'activated', 'success')
log = logging.getLogger('browser_broker')


class NotConnected(RuntimeError):
    pass


class CdpError(RuntimeError):
    pass


# Reading a page

_FIND = '''const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
const byId = name => [...document.querySelectorAll(`[data-automation-id="${name}"]`)].find(visible) || null;
const field = name => { const el = byId(name); return !el || el.matches('input,textarea') ? el : el.querySelector('input,textarea'); };'''

SNAPSHOT = '(() => {' + _FIND + '''
const frames = [...document.querySelectorAll('iframe')].filter(visible).map(f => `${f.src || ''} ${f.title || ''}`);
const text = (document.body ? document.body.innerText : '').slice(0, 8000);
const captcha = frames.some(s => /recaptcha|hcaptcha|turnstile|challenges\\.cloudflare|arkoselabs|funcaptcha/i.test(s) && !/size=invisible/i.test(s))
  || [...document.querySelectorAll('.g-recaptcha,.h-captcha,.cf-turnstile')].some(e => visible(e) && e.getAttribute('data-size') !== 'invisible')
  || /verify you are human/i.test(text);
const errors = [...document.querySelectorAll('[data-automation-id*="error" i],[role="alert"]')].filter(visible)
  .map(e => e.innerText.trim()).filter(Boolean).slice(0, 5);
return {url: location.href, ready: document.readyState === 'complete', captcha, text, errors,
  password: [...document.querySelectorAll('input[type="password"]')].some(visible),
  create_form: !!byId('createAccountSubmitButton'), sign_in_form: !!byId('signInSubmitButton'),
  create_link: !!byId('createAccountLink'), sign_in_link: !!byId('signInLink')};
})()'''


def classify(snapshot, mode):
    """What a page shows after a create or sign-in submit: (outcome, reason), or (None, '') while it is still on the form."""
    if snapshot['captcha']:
        return 'captcha', ''
    errors = ' '.join(snapshot['errors']).lower()
    text = snapshot['text'].lower()
    if 'already' in errors and any(word in errors for word in ('exist', 'in use', 'registered', 'associated')):
        return 'email_in_use', ''
    if mode == 'sign_in' and 'password' in errors and any(word in errors for word in ('wrong', 'invalid', 'incorrect')):
        return 'wrong_password', ''
    if any(phrase in errors or phrase in text for phrase in VERIFY_PHRASES):
        return 'verification_sent', ''
    if snapshot['ready'] and text.strip() and not (snapshot['password'] or snapshot['create_form'] or snapshot['sign_in_form']):
        return 'signed_in', ''
    if snapshot['errors']:
        return 'unknown_page', snapshot['errors'][0][:200]
    return None, ''


# DevTools

class Cdp:
    """Commands over one browser-level DevTools websocket. Messages are never logged: Input.insertText carries the password."""

    def __init__(self, ws):
        self.ws, self.next_id = ws, 0

    def call(self, method, params=None, session=None, timeout=30.0):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            reply = json.loads(self.ws.recv(timeout=max(0.0, deadline - time.monotonic())))
            if reply.get('id') == self.next_id:
                if 'error' in reply:
                    raise CdpError(f"{method} failed: {reply['error'].get('message', 'unknown error')}")
                return reply.get('result', {})

    def close(self):
        self.ws.close()


def connect_websocket(url, timeout):
    from websockets.sync.client import connect
    return connect(url, open_timeout=timeout, ping_interval=None, compression=None, proxy=None, max_size=None, max_queue=None)


class Chromium:
    """Keeps one connection to the applying browser, and opens a new one when a request finds it gone."""

    def __init__(self, browser_url=BROWSER_URL, connect=connect_websocket, timeout=10.0):
        self.browser_url, self.connect, self.timeout = browser_url.rstrip('/'), connect, timeout
        self.cdp = None

    def session(self):
        if self.cdp is None:
            try:
                # The browser's id, and so its websocket path, changes on every start.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'{self.browser_url}/json/version', timeout=self.timeout) as reply:
                    endpoint = json.load(reply)['webSocketDebuggerUrl']
                self.cdp = Cdp(self.connect(endpoint, self.timeout))
                log.info('connected to Chromium')
            except Exception as exc:  # noqa: BLE001 - any failure leaves no connection, and the next request tries again
                log.warning('no Chromium connection: %s', type(exc).__name__)
                raise NotConnected('the applying browser is not running') from exc
        return self.cdp

    def drop(self):
        cdp, self.cdp = self.cdp, None
        if cdp is not None:
            try:
                cdp.close()
            except Exception:  # noqa: BLE001 - the connection is already gone
                pass


class Page:
    """One attached tab."""

    def __init__(self, cdp, session):
        self.cdp, self.session = cdp, session

    def evaluate(self, expression):
        result = self.cdp.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, self.session)
        if 'exceptionDetails' in result:
            raise CdpError('a page script failed')
        return result['result'].get('value')

    def snapshot(self):
        return self.evaluate(SNAPSHOT)

    def fill(self, automation_id, value, *, secret=False):
        """Type value into the field the way a user's keyboard would, then read it back. A secret is checked by length only."""
        name = json.dumps(automation_id)
        if not self.evaluate(f'(() => {{{_FIND} const el = field({name}); if (!el) return false; el.scrollIntoView({{block: "center"}}); '
                             'el.focus(); el.select(); return document.activeElement === el; })()'):
            return False
        self.cdp.call('Input.insertText', {'text': value}, self.session)
        check = f'el.value.length === {len(value)}' if secret else f'el.value === {json.dumps(value)}'
        return bool(self.evaluate(f'(() => {{{_FIND} const el = field({name}); return !!el && {check}; }})()'))

    def click(self, automation_id):
        """Click a Workday control. Workday covers its buttons with a click_filter overlay, which is what takes the click."""
        name = json.dumps(automation_id)
        return bool(self.evaluate(f'(() => {{{_FIND} const el = byId({name}); if (!el) return false; el.scrollIntoView({{block: "center"}}); '
                                  'const filter = el.parentElement && el.parentElement.querySelector(\'[data-automation-id="click_filter"]\'); '
                                  '(filter || el).click(); return true; })()'))

    def check(self, automation_id):
        name = json.dumps(automation_id)
        return self.evaluate(f'(() => {{{_FIND} const el = field({name}) || byId({name}); if (!el) return null; '
                             'const box = el.matches("input") ? el : el.querySelector("input[type=checkbox]"); if (!box) return null; '
                             'if (!box.checked) box.click(); return box.checked; })()')

    def wait(self, done, timeout, interval=0.5):
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot()
            if done(snapshot) or time.monotonic() > deadline:
                return snapshot
            time.sleep(interval)


# Sign-in hosts of the user's personal accounts. The shared password is for job-site accounts only and is never typed there.
PERSONAL_HOSTS = ('google.com', 'live.com', 'microsoftonline.com', 'microsoft.com', 'apple.com', 'linkedin.com', 'facebook.com', 'github.com',
                  'twitter.com', 'x.com', 'amazon.com', 'yahoo.com', 'okta.com', 'duosecurity.com')
# Microsoft's own careers site lives under microsoft.com and keeps its accounts apart from a personal Microsoft account.
JOB_HOSTS = ('careers.microsoft.com',)


def account_host(url):
    """The host of an https page where the shared password may be typed, or None for any other page."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError, AttributeError):
        return None
    if parts.scheme != 'https' or parts.username or parts.password or not parts.hostname or port not in (None, 443):
        return None
    host = parts.hostname.lower()
    under = lambda names: any(host == name or host.endswith('.' + name) for name in names)  # noqa: E731
    return None if under(PERSONAL_HOSTS) and not under(JOB_HOSTS) else host


_PASSWORD_FIELDS = "[...document.querySelectorAll('input[type=\"password\"]')].filter(visible)"


def passwords_on_page(page, password):
    """Type the shared password into every visible password field of the page, such as Password and Confirm password."""
    snapshot = page.snapshot()
    if snapshot['captcha']:
        return {'outcome': 'captcha'}
    count = page.evaluate(f'(() => {{{_FIND} return {_PASSWORD_FIELDS}.length; }})()') or 0
    if not count:
        return {'outcome': 'no_password_field', 'reason': 'the page shows no password field'}
    for index in range(count):
        if not page.evaluate(f'(() => {{{_FIND} const el = {_PASSWORD_FIELDS}[{index}]; if (!el) return false; el.scrollIntoView({{block: "center"}}); '
                             'el.focus(); el.select(); return document.activeElement === el; })()'):
            return {'outcome': 'unknown_page', 'reason': f'could not focus password field {index + 1}'}
        page.cdp.call('Input.insertText', {'text': password}, page.session)
        if not page.evaluate(f'(() => {{{_FIND} const el = {_PASSWORD_FIELDS}[{index}]; return !!el && el.value.length === {len(password)}; }})()'):
            return {'outcome': 'unknown_page', 'reason': f'password field {index + 1} did not take the text'}
    return {'outcome': 'filled', 'fields': count}


FORMS = {
    'create': {'form': 'create_form', 'link': 'createAccountLink', 'fields': ('email', 'password', 'verifyPassword'),
               'checkbox': 'createAccountCheckbox', 'submit': 'createAccountSubmitButton'},
    'sign_in': {'form': 'sign_in_form', 'link': 'signInLink', 'fields': ('email', 'password'),
                'checkbox': None, 'submit': 'signInSubmitButton'},
}


def account_on_page(page, email, password, mode, *, settle=25.0):
    """Create an account or sign in on a Workday page, and report what the page shows afterwards."""
    form = FORMS[mode]
    snapshot = page.snapshot()
    if snapshot['captcha']:
        return {'outcome': 'captcha'}
    if not snapshot[form['form']]:
        if not page.click(form['link']):
            return {'outcome': 'unknown_page', 'reason': f'the page has no {mode.replace("_", " ")} form or link'}
        snapshot = page.wait(lambda s: s[form['form']] or s['captcha'], 10)
        if snapshot['captcha']:
            return {'outcome': 'captcha'}
        if not snapshot[form['form']]:
            return {'outcome': 'unknown_page', 'reason': f'the {mode.replace("_", " ")} form did not open'}
    for name in form['fields']:
        if not page.fill(name, email if name == 'email' else password, secret=name != 'email'):
            return {'outcome': 'unknown_page', 'reason': f'could not fill the {name} field'}
    if form['checkbox'] and page.check(form['checkbox']) is False:
        return {'outcome': 'unknown_page', 'reason': 'could not tick the account agreement checkbox'}
    if not page.click(form['submit']):
        return {'outcome': 'unknown_page', 'reason': 'could not find the submit button'}
    time.sleep(1)
    deadline = time.monotonic() + settle
    while True:
        outcome, reason = classify(page.snapshot(), mode)
        if outcome == 'signed_in':
            # A page between navigations can look signed in for a moment, so it must hold.
            time.sleep(1.5)
            outcome, reason = classify(page.snapshot(), mode)
        if outcome is not None:
            return {'outcome': outcome, **({'reason': reason} if reason else {})}
        if time.monotonic() > deadline:
            return {'outcome': 'unknown_page', 'reason': 'the page showed no result after submit'}
        time.sleep(1)


class Broker:
    def __init__(self, root, chromium):
        self.root, self.chromium = Path(root), chromium

    def handle(self, request):
        """Answer one request. Everything in it is checked here, at the socket."""
        if not isinstance(request, dict):
            return {'outcome': 'invalid', 'reason': 'a request is a JSON object'}
        kind = request.get('type')
        try:
            if kind == 'workday_account':
                page_url, email, mode = request.get('page_url'), request.get('email'), request.get('mode')
                if workday_site(page_url) is None or not isinstance(email, str) or not _EMAIL.fullmatch(email) or mode not in MODES:
                    return {'outcome': 'invalid', 'reason': 'workday_account needs a Workday page_url, an email and a mode'}
                result = self.workday_account(page_url, email, mode)
            elif kind == 'fill_password':
                page_url = request.get('page_url')
                if account_host(page_url) is None:
                    return {'outcome': 'personal_account', 'reason': 'the shared password is never typed on this page'}
                result = self._on_tab(page_url, passwords_on_page)
            elif kind == 'open_link':
                url, expected = request.get('url'), request.get('expected_site')
                site = workday_site(url)
                if site is None or site.key != expected:
                    return {'outcome': 'invalid', 'reason': 'the link is not on the expected Workday site'}
                result = self.open_link(url, site)
            else:
                return {'outcome': 'invalid', 'reason': 'unknown request type'}
        except NotConnected:
            result = {'outcome': 'not_connected'}
        except PasswordError as exc:
            result = {'outcome': 'no_password', 'reason': str(exc)}
        except (CdpError, TimeoutError, OSError) as exc:
            result = {'outcome': 'unknown_page', 'reason': str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001 - a closed websocket raises its own types; the next request reconnects
            self.chromium.drop()
            result = {'outcome': 'not_connected', 'reason': type(exc).__name__}
        log.info('%s for job %s: %s', kind, request.get('job_id'), result['outcome'])
        return result

    @contextmanager
    def _attached(self, target_id):
        cdp = self.chromium.session()
        session = cdp.call('Target.attachToTarget', {'targetId': target_id, 'flatten': True})['sessionId']
        try:
            yield Page(cdp, session)
        finally:
            try:
                cdp.call('Target.detachFromTarget', {'sessionId': session})
            except CdpError:
                pass

    def workday_account(self, page_url, email, mode):
        password = read_password(self.root)
        if password is None:
            return {'outcome': 'no_password'}
        return self._on_tab(page_url, lambda page, password: account_on_page(page, email, password, mode), password)

    def _on_tab(self, page_url, act, password=None):
        """Run act(page, password) on the one tab that shows exactly page_url."""
        password = password or read_password(self.root)
        if password is None:
            return {'outcome': 'no_password'}
        cdp = self.chromium.session()
        tabs = [t for t in cdp.call('Target.getTargets')['targetInfos'] if t.get('type') == 'page' and t.get('url') == page_url]
        if len(tabs) != 1:
            return {'outcome': 'tab_not_found', 'reason': f'{len(tabs)} browser tabs show exactly this URL, and it needs one.'}
        with self._attached(tabs[0]['targetId']) as page:
            return act(page, password)

    def open_link(self, url, site):
        """Open a verification link in a new background tab, read what it says, and close the tab again."""
        cdp = self.chromium.session()
        target = cdp.call('Target.createTarget', {'url': url, 'background': True})['targetId']
        try:
            with self._attached(target) as page:
                snapshot = page.wait(lambda s: s['ready'] and s['text'].strip(), 20)
                time.sleep(2)
                snapshot = page.snapshot()
        finally:
            cdp.call('Target.closeTarget', {'targetId': target})
        if workday_site(snapshot['url']) != site:
            return {'outcome': 'unknown_page', 'reason': 'the link led off the Workday site'}
        if snapshot['captcha']:
            return {'outcome': 'captcha'}
        if any(word in snapshot['text'].lower() for word in VERIFIED_WORDS):
            return {'outcome': 'verified'}
        return {'outcome': 'unknown_page', 'reason': 'the verification page did not say the account was verified'}


# The socket

@contextmanager
def socket_path(root):
    """A short path to the socket. The real one can exceed the 108 bytes a unix socket path allows, so it goes
    through an open descriptor of its directory."""
    directory = Path(root).resolve() / '.pipeline'
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield f'/proc/self/fd/{fd}/{SOCKET}'
    finally:
        os.close(fd)


def serve(root, broker, *, ready=None, stop=None):
    lock = open(Path(root).resolve() / '.pipeline' / 'browser-broker.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another account program is already running') from None
    with socket_path(root) as path, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        previous = os.umask(0o177)
        try:
            server.bind(path)
        finally:
            os.umask(previous)
        server.listen(8)
        server.settimeout(0.5)
        if ready is not None:
            ready.set()
        while stop is None or not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(10)
                try:
                    raw = _read_line(connection)
                    reply = broker.handle(json.loads(raw))
                except (ValueError, OSError):
                    reply = {'outcome': 'invalid', 'reason': 'a request is one JSON line'}
                try:
                    connection.sendall(json.dumps(reply).encode() + b'\n')
                except OSError:
                    pass
    lock.close()


def _read_line(connection):
    data = b''
    while not data.endswith(b'\n'):
        chunk = connection.recv(4096)
        if not chunk or len(data) + len(chunk) > MAX_REQUEST:
            raise ValueError('incomplete request')
        data += chunk
    return data


class BrokerClient:
    """What an Applier's tool server uses. A missing account program is an outcome, not an exception."""

    def __init__(self, root, timeout=180.0):
        self.root, self.timeout = root, timeout

    def request(self, payload):
        try:
            with socket_path(self.root) as path, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(path)
                client.sendall(json.dumps(payload).encode() + b'\n')
                data = b''
                while not data.endswith(b'\n'):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except (FileNotFoundError, ConnectionRefusedError):
            return {'outcome': 'broker_unavailable'}
        except TimeoutError:
            return {'outcome': 'unknown_page', 'reason': 'the account program did not answer in time'}
        try:
            return json.loads(data)
        except ValueError:
            return {'outcome': 'unknown_page', 'reason': 'the account program gave no answer'}

    def workday_account(self, job_id, page_url, email, mode):
        return self.request({'type': 'workday_account', 'job_id': job_id, 'page_url': page_url, 'email': email, 'mode': mode})

    def fill_password(self, job_id, page_url):
        return self.request({'type': 'fill_password', 'job_id': job_id, 'page_url': page_url})

    def open_link(self, url, expected_site):
        return self.request({'type': 'open_link', 'url': url, 'expected_site': expected_site})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('serve')
    run.add_argument('--root', type=Path, required=True)
    run.add_argument('--browser-url', default=BROWSER_URL)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stderr)
    serve(args.root, Broker(args.root, Chromium(args.browser_url)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
