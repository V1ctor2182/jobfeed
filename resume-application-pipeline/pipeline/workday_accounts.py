"""Workday candidate accounts that a program, never a model, creates and signs in to.

The one shared password lives in .pipeline/account-password, mode 0600. Only the browser broker reads it, and it
never enters a model, a log, a tool result or the account store.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import html
import json
import os
from email.utils import parseaddr
from pathlib import Path
import re
import shlex
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

SYSTEM = 'workday'
PASSWORD_FILE = Path('.pipeline/account-password')
STATES = frozenset({'created', 'verified', 'signed_in', 'failed'})
_JOBS_HOST = re.compile(r'(?P<tenant>[a-z0-9-]+)\.[a-z0-9-]+\.myworkdayjobs\.com')
_SITE_HOST = re.compile(r'wd[0-9]+\.myworkdaysite\.com')
_TENANT = re.compile(r'[a-z0-9_-]+')
_URL = re.compile(r'https://[^\s<>"\'()\[\]]+')
_VERIFY_WORDS = ('activate', 'verif', 'confirm')
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS accounts(
 site TEXT PRIMARY KEY, system TEXT NOT NULL, tenant TEXT NOT NULL, email TEXT NOT NULL, state TEXT NOT NULL,
 created_by_job INTEGER, job_id INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT);
'''


@dataclass(frozen=True)
class WorkdaySite:
    """One Workday tenant's candidate site. An account belongs to exactly one.

    key is the host on a tenant's own myworkdayjobs.com host, and host/recruiting/tenant on the myworkdaysite.com
    hosts that many tenants share.
    """
    key: str
    tenant: str

    @property
    def senders(self):
        """Addresses Workday sends this tenant's account mail from. Verification mail comes from otp.workday.com."""
        return (f'{self.tenant}@myworkday.com', f'{self.tenant}@otp.workday.com')


def workday_site(url):
    """The Workday site an https URL belongs to, or None for any other page."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError, AttributeError):
        return None
    if parts.scheme != 'https' or parts.username or parts.password or not parts.hostname or port not in (None, 443):
        return None
    host = parts.hostname
    if match := _JOBS_HOST.fullmatch(host):
        return WorkdaySite(host, match['tenant'])
    if _SITE_HOST.fullmatch(host):
        segments = [s.lower() for s in parts.path.split('/') if s]
        if 'recruiting' in segments[:-1] and _TENANT.fullmatch(tenant := segments[segments.index('recruiting') + 1]):
            return WorkdaySite(f'{host}/recruiting/{tenant}', tenant)
    return None


def _now():
    return datetime.now(timezone.utc).isoformat()


# The shared password

class PasswordError(ValueError):
    pass


def password_problem(password):
    """Why Workday would refuse the password, or None. Printable ASCII only, so its length is the same in the page."""
    if len(password) < 8:
        return 'it needs at least 8 characters'
    if any(c not in string.printable or c in string.whitespace for c in password):
        return 'use printable ASCII characters without spaces'
    for label, chars in (('an uppercase letter', string.ascii_uppercase), ('a lowercase letter', string.ascii_lowercase),
                         ('a digit', string.digits), ('a special character', string.punctuation)):
        if not any(c in chars for c in password):
            return f'it needs {label}'
    return None


def save_password(root, password):
    if problem := password_problem(password):
        raise PasswordError(f'The password was not saved: {problem}.')
    path = Path(root) / PASSWORD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(password)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_password(root):
    """The shared password, or None when none is set. Refuses a file that anyone but its owner can read."""
    try:
        fd = os.open(Path(root) / PASSWORD_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as handle:
        status = os.fstat(handle.fileno())
        if status.st_mode & 0o077 or status.st_uid != os.getuid():
            raise PasswordError(f'{PASSWORD_FILE} must be owned by you with mode 0600')
        return handle.read() or None


def set_password(root, ask=getpass.getpass):
    first = ask('Shared account password: ')
    if problem := password_problem(first):
        raise PasswordError(f'The password was not saved: {problem}.')
    if ask('Type it again: ') != first:
        raise PasswordError('The two passwords differ. Nothing was saved.')
    save_password(root, first)


# Accounts

class AccountStore:
    """One row per Workday site: the email used, how far the account got, and the job that created it. No password."""

    def __init__(self, root):
        self.path = Path(root) / '.pipeline' / 'accounts.sqlite3'

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        return db

    def get(self, key):
        with closing(self._db()) as db:
            row = db.execute('SELECT * FROM accounts WHERE site=?', (key,)).fetchone()
        return None if row is None else dict(row)

    def record(self, site, *, email, state, job_id, created=False, error=None):
        """Save what the latest step showed. created marks this job as the one that created the account, once."""
        if state not in STATES:
            raise ValueError(f'unknown account state {state!r}')
        now = _now()
        with closing(self._db()) as db, db:
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(site) DO UPDATE SET email=excluded.email,state=excluded.state,'
                       'created_by_job=COALESCE(accounts.created_by_job,excluded.created_by_job),job_id=excluded.job_id,updated_at=excluded.updated_at,'
                       'last_error=excluded.last_error',
                       (site.key, SYSTEM, site.tenant, email, state, job_id if created else None, job_id, now, now, error))

    def note_error(self, key, job_id, error):
        with closing(self._db()) as db, db:
            db.execute('UPDATE accounts SET last_error=?,job_id=?,updated_at=? WHERE site=?', (error, job_id, _now(), key))

    def accounts(self):
        if not self.path.exists():
            return []
        with closing(self._db()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM accounts ORDER BY site')]

    def created_by(self, job_id):
        return [{'site': a['site'], 'email': a['email']} for a in self.accounts() if a['created_by_job'] == job_id]

    def forget(self, key):
        with closing(self._db()) as db, db:
            return db.execute('DELETE FROM accounts WHERE site=?', (key,)).rowcount == 1


# The verification email

def verification_link(text, site):
    """The one verification link in an email for this site. Email content is untrusted: a link to any other site,
    or more than one candidate, is refused."""
    links = {link.rstrip('.,;:!?') for link in _URL.findall(html.unescape(text or ''))}
    found = {link for link in links if workday_site(link) == site and any(word in link.lower() for word in _VERIFY_WORDS)}
    return found.pop() if len(found) == 1 else None


_SEARCH_TOOL = 'mcp__claude_ai_Gmail__search_threads'
_THREAD_TOOL = 'mcp__claude_ai_Gmail__get_thread'
_PROMPT_ARGUMENTS = {_SEARCH_TOOL: '{"query":"x"}', _THREAD_TOOL: '{"threadId":"x"}'}
_COURIER_TIMEOUT = 170


class GmailError(RuntimeError):
    pass


def gmail_call(tool, tool_input):
    """One Gmail connector call through `claude -p`, as the recruiter sheet reaches Google. A hook replaces whatever
    input the model sends with tool_input, and only the raw tool result is read, so the email cannot steer it."""
    from jobfeed.adapters import sheet_courier_hook
    from jobfeed.adapters.claude_stream import tool_results
    with tempfile.TemporaryDirectory(prefix='workday-gmail-') as directory:
        plan = Path(directory) / 'plan.json'
        plan.write_text(json.dumps({'tool': tool, 'input': tool_input}))
        settings = Path(directory) / 'settings.json'
        hook = shlex.join([sys.executable, '-I', str(Path(sheet_courier_hook.__file__).resolve()), str(plan)])
        settings.write_text(json.dumps({'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [{'type': 'command', 'command': hook}]}]}}))
        prompt = (f'Call the Gmail {tool.rsplit("__", 1)[1]} tool exactly once with arguments {_PROMPT_ARGUMENTS[tool]}. '
                  'Then reply with just the word done.')
        try:
            done = subprocess.run(['claude', '-p', prompt, '--model', 'haiku', '--allowedTools', tool, '--settings', str(settings),
                                   '--setting-sources', '', '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
                                   '--max-budget-usd', '0.25'],
                                  cwd=directory, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_COURIER_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GmailError(f'claude -p did not run: {type(exc).__name__}') from None
    results, _cost = tool_results(done.stdout, tool)
    for result in results:
        if not result.get('is_error'):
            content = result.get('content')
            text = content if isinstance(content, str) else ''.join(b.get('text', '') for b in content or () if isinstance(b, dict))
            try:
                return json.loads(text)
            except ValueError:
                raise GmailError('the Gmail result was not JSON') from None
    raise GmailError(f'the Gmail tool returned no result (claude exited {done.returncode})')


def read_threads(hits, call, limit, message_format='PLAIN_TEXT', workers=1):
    """The full messages for these search hits, newest first, each read from its thread.

    Since 2026-11-21 a Gmail search returns no message time, preview or display names; get_thread still does. Each
    thread is read once, for at most limit threads in search order, up to workers at a time. A thread the courier
    cannot return is skipped. A field the thread leaves out keeps its value from the search hit."""
    from concurrent.futures import ThreadPoolExecutor
    by_id = {m['id']: m for m in hits if m.get('id')}
    threads = list(dict.fromkeys(m.get('threadId') or m['id'] for m in by_id.values()))[:limit]

    def read(thread_id):
        try:
            return call(_THREAD_TOOL, {'threadId': thread_id, 'messageFormat': message_format}).get('messages') or ()
        except GmailError:
            return ()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        messages = [m for found in pool.map(read, threads) for m in found]
    full = [{**by_id[m['id']], **m} for m in messages if m.get('id') in by_id]
    return sorted(full, key=lambda m: int(m.get('internalDate') or 0), reverse=True)


def find_verification_link(site, email, since, call=gmail_call):
    """The verification link in the newest email from the site's tenant to email that arrived after since (epoch seconds)."""
    senders = ' OR '.join(f'from:{sender}' for sender in site.senders)
    found = call(_SEARCH_TOOL, {'query': f'({senders}) to:{email} newer_than:2d', 'pageSize': 10, 'view': 'THREAD_VIEW_METADATA_ONLY'})
    hits = [m for thread in found.get('threads') or () for m in thread.get('messages') or ()
            if parseaddr(m.get('sender', ''))[1].lower() in site.senders]
    for message in read_threads(hits, call, limit=5):
        if int(message.get('internalDate') or 0) >= since * 1000 and (link := verification_link(message.get('plaintextBody') or '', site)):
            return link
    return None


# One account step for an Applier

def _report(summary):
    return ('Call report_blocker with blocker_type user_only_check, the page URL in page_url, and summary "' + summary + '". '
            'When the user replies, call account_step again. Never type an email or password on this page yourself.')


# The final outcome of a step: the account state it proves, or None when it proves nothing about the account, and the next instruction.
OUTCOMES = {
    'signed_in': ('signed_in', 'The account program signed in to {site} as {email}. Call tabs_context_mcp, find your registered tab, and continue '
                               'filling the application.'),
    'wrong_password': ('failed', _report('An account for {email} already exists on {site}, and the shared password does not open it. '
                                         'Sign in on the tab yourself, or reset its password to the shared one.')),
    'needs_verification': (None, _report('Workday needs {email} verified on {site}, and the account program found no usable '
                                         'verification email from {senders}. Open that email\'s link, then reply.')),
    'captcha': (None, 'The page shows a CAPTCHA. Call report_blocker with blocker_type captcha and the page URL in page_url, then do what it returns.'),
    'unknown_page': (None, _report('The account program could not sign in on {site}: {reason}')),
    'tab_not_found': (None, '{reason} Call tabs_context_mcp and call account_step once more with your registered tab\'s exact current URL. '
                            'If that fails too, report it: ' + _report('The account program could not find the form tab on {site}.')),
    'not_connected': (None, _report('The account program cannot reach the applying browser. Start it with systemctl --user start '
                                    'jobfeed-apply-chromium, then reply.')),
    'broker_unavailable': (None, _report('The account program is not running. Start it with systemctl --user start '
                                         'jobfeed-browser-broker, then reply.')),
    'no_password': (None, _report('No shared account password is set. Run python -m pipeline.cli accounts set-password, then reply.')),
}
# Outcomes the user must fix outside the job, posted as a Discord alert too.
ALERTS = frozenset({'not_connected', 'broker_unavailable', 'no_password'})


def account_step(broker, store, site, email, job_id, page_url, find_link=find_verification_link, *, polls=6, wait=20.0, sleep=time.sleep, clock=time.time):
    """Sign in on the Applier's tab, creating and verifying the account first when it needs that.

    broker drives the browser; find_link(site, email, since) reads the verification email. Returns the outcome and the next instruction.
    """
    account = store.get(site.key)
    mode = 'sign_in' if account and account['state'] != 'failed' else 'create'
    since = clock()
    result = broker.workday_account(job_id, page_url, email, mode)
    if result['outcome'] == 'email_in_use':
        store.record(site, email=email, state='failed', job_id=job_id, error='email_in_use')
        result = broker.workday_account(job_id, page_url, email, 'sign_in')
    elif mode == 'create' and result['outcome'] in {'verification_sent', 'signed_in'}:
        store.record(site, email=email, state='created', job_id=job_id, created=True)
    if result['outcome'] == 'verification_sent':
        result = _verify(broker, store, site, email, job_id, page_url, find_link, since, polls=polls, wait=wait, sleep=sleep)
    outcome, reason = result['outcome'], result.get('reason', '')
    if outcome not in OUTCOMES:
        outcome, reason = 'unknown_page', f'unexpected result {outcome}'
    state, instruction = OUTCOMES[outcome]
    if state is not None:
        store.record(site, email=email, state=state, job_id=job_id, error=None if state == 'signed_in' else outcome)
    elif store.get(site.key) is not None:
        store.note_error(site.key, job_id, f'{outcome}: {reason}'.rstrip(': '))
    return {'outcome': outcome, 'site': site.key, 'email': email,
            'next': instruction.format(site=site.key, email=email, senders=' or '.join(site.senders), reason=reason or 'no reason shown')}


def _verify(broker, store, site, email, job_id, page_url, find_link, since, *, polls, wait, sleep):
    link = None
    for _ in range(polls):
        sleep(wait)
        try:
            link = find_link(site, email, since - 60)
        except GmailError:
            link = None
        if link:
            break
    if link is None:
        return {'outcome': 'needs_verification'}
    opened = broker.open_link(link, site.key)
    if opened['outcome'] == 'verified':
        store.record(site, email=email, state='verified', job_id=job_id)
    elif opened['outcome'] in ALERTS:
        return opened
    result = broker.workday_account(job_id, page_url, email, 'sign_in')
    return {'outcome': 'needs_verification'} if result['outcome'] == 'verification_sent' else result


def execute(root, args, ask=getpass.getpass, stdin=None):
    """The `accounts` CLI. Nothing here prints or returns the password."""
    if args.accounts_command == 'set-password':
        set_password(root, ask)
        return {'saved': True, 'path': str(Path(root) / PASSWORD_FILE)}
    if args.accounts_command == 'save-password':
        # The Dashboard passes the password on standard input, so it is never an argument another process can list.
        save_password(root, (stdin or sys.stdin).read().rstrip('\r\n'))
        return {'saved': True, 'password_set': True, 'accounts': len(AccountStore(root).accounts())}
    store = AccountStore(root)
    if args.accounts_command == 'list':
        return {'password_set': (Path(root) / PASSWORD_FILE).exists(), 'accounts': store.accounts()}
    return {'site': args.site, 'forgotten': store.forget(args.site)}


def register(commands):
    accounts = commands.add_parser('accounts', help='Workday accounts the account program created, and the one shared password it uses')
    sub = accounts.add_subparsers(dest='accounts_command', required=True)
    sub.add_parser('set-password', help='Set the shared password. It is read twice and never shown')
    sub.add_parser('save-password', help='Save the shared password read once from standard input. It is never shown')
    sub.add_parser('list', help='List the accounts, without the password')
    sub.add_parser('forget', help='Forget one site\'s account record, so the next Applier creates or signs in again').add_argument('site')
