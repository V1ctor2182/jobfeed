"""Read the verification code a site emailed the user, so an Applier can type it without asking the user.

The mailbox is reached as the account program reaches it: through `claude -p`, reading only the raw tool result. Email
content is untrusted. A message counts only when it arrived after the page asked for the code and names the site or
the employer, and only digits near a code word are returned, never any other text of the message.
"""
from __future__ import annotations

from email.utils import parseaddr
import html
import re
import time
from urllib.parse import urlsplit

from pipeline import workday_accounts

SEARCH = workday_accounts._SEARCH_TOOL
THREAD = workday_accounts._THREAD_TOOL
# A code is used within minutes of being sent.
LOOK_BACK = 20 * 60
MAX_MESSAGES = 3
_WORDS = r'(?:code|passcode|pass code|pin|otp|one[- ]time|verification|verify|验证码)'
_CODE = re.compile(r'(?<![\w.,/#@$-])(\d{4,8})(?![\w/@%-]|[.,:]\d)')
_LINK_WORDS = ('activ', 'verif', 'confirm', 'validat', 'registration', 'token')
_YEAR = re.compile(r'(?:19|20)\d\d')
_NEAR = re.compile(_WORDS, re.I)
# Second-level labels that name a host's country or kind, not its owner.
_GENERIC = frozenset({'com', 'co', 'org', 'net', 'gov', 'edu', 'www', 'jobs', 'careers', 'apply', 'app', 'login', 'auth', 'accounts', 'id', 'mail'})


def names(company, page_url):
    """Lowercase words that tie an email to this application: the company's name, the page host's own labels and its site.

    The site (adp.com) ties a code that names only the platform, sent from its own domain, whose name is too short to be a label.
    """
    found = {company.strip().lower()} if company and len(company.strip()) >= 3 else set()
    host = (urlsplit(page_url).hostname or '').lower()
    found |= {label for label in host.split('.')[:-1] if len(label) >= 4 and label not in _GENERIC}
    site = site_of(host)
    if site.count('.') == 1 and site.split('.')[0] not in _GENERIC:
        found.add(site)
    return found


def code_in(text):
    """The one code in an email's text: digits within a short distance of a code word. Several different candidates mean none."""
    text = html.unescape(text or '')
    found, after_word = set(), set()
    for match in _CODE.finditer(text):
        if _NEAR.search(text[max(0, match.start() - 120):match.end() + 60]):
            found.add(match.group(1))
        # "this code: 123456" names the code itself; a job or order number that merely stands near the word does not.
        if _NEAR.search(text[max(0, match.start() - 30):match.start()]):
            after_word.add(match.group(1))
    if len(after_word) == 1:
        return after_word.pop()
    if len(found) > 1:
        found = {code for code in found if not _YEAR.fullmatch(code)}
    return found.pop() if len(found) == 1 else None


def _recent(email, since, words, call):
    """The newest messages to this address after since, each with its sender, subject and plain-text body."""
    found = call(SEARCH, {'query': f'to:{email} newer_than:1d ({words})', 'pageSize': 10, 'view': 'THREAD_VIEW_METADATA_ONLY'})
    hits = [m for thread in found.get('threads') or () for m in thread.get('messages') or ()]
    # A thread the courier cannot return, such as one large message, is skipped and does not hide the code in another.
    messages = [m for m in workday_accounts.read_threads(hits, call, limit=MAX_MESSAGES) if int(m.get('internalDate') or 0) >= since * 1000]
    for message in messages[:MAX_MESSAGES]:
        yield message.get('sender') or '', message.get('subject') or '', message.get('plaintextBody') or ''


def site_of(host):
    """The part of a host that names its owner: its last two labels. Good enough to keep a link on the site that asked for it."""
    return '.'.join((host or '').lower().split('.')[-2:])


def link_in(text, site):
    """The one account verification link in an email that stays on site. A link to any other site, or several candidates, is refused."""
    links = {link.rstrip('.,;:!?') for link in workday_accounts._URL.findall(html.unescape(text or ''))}
    found = {link for link in links if site_of(urlsplit(link).hostname) == site and any(word in link.lower() for word in _LINK_WORDS)}
    return found.pop() if len(found) == 1 else None


def find_link(email, since, site, call=workday_accounts.gmail_call):
    """The verification link, on this site only, in the newest email to this address that arrived after since."""
    for _sender, _subject, body in _recent(email, since, 'verify OR verification OR confirm OR activate OR activation', call):
        if link := link_in(body, site):
            return link
    return None


def find_code(email, since, tied_to, call=workday_accounts.gmail_call):
    """The code in the newest email to this address that arrived after since (epoch seconds) and mentions one of tied_to."""
    for sender, subject, body in _recent(email, since, 'code OR verification OR verify OR passcode OR "one-time"', call):
        everything = ' '.join((sender, parseaddr(sender)[1], subject, body)).lower()
        if not any(name in everything for name in tied_to):
            continue
        if code := code_in(subject + '\n' + body):
            return code
    return None


def wait_for_link(email, site, *, find=find_link, **options):
    """Poll the mailbox for a verification link on site sent in the last LOOK_BACK seconds."""
    return wait_for_code(email, site, find=find, **options)


def wait_for_code(email, tied_to, *, find=find_code, polls=4, wait=15.0, sleep=time.sleep, clock=time.time):
    """Poll the mailbox for a code sent in the last LOOK_BACK seconds. Returns the code, or None when none arrived."""
    since = clock() - LOOK_BACK
    for attempt in range(polls):
        if attempt:
            sleep(wait)
        try:
            code = find(email, since, tied_to)
        except workday_accounts.GmailError:
            code = None
        if code:
            return code
    return None
