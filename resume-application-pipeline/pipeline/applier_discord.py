"""Discord bridge for Claude Appliers: one thread per job, answers accepted only from the allowed account.

Questions, blockers and review summaries are embeds with buttons. On a question with a draft, Approve draft saves it
as written, Request changes sends feedback for a new draft, and Write my own opens a form prefilled with the draft.
Submit and Ignore ask once more before they act. The gateway listener delivers
the presses. Plain replies still work through REST polling, so the bridge keeps working without the listener.
The bot, not a model, records who answered: each saved reply carries the Discord message or interaction ID and
the sender ID.

Submit, by button or by a reply of "submit" to the newest summary, runs the Dashboard's own Submit confirmation,
so Discord is a second way to press the same button. Under the auto submit mode the user turned on, the bridge runs that
confirmation itself for each summary it may confirm, and says so in the job's thread.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

from pipeline.answer_bank import AnswerBank, BankError, applier_decided
from pipeline.applier_fixer import Fixer
from pipeline.applier_runtime import RETRYABLE_SUBMIT, ApplierRuntime
from pipeline.confirmation_mail import ask_user_now
from pipeline.jobfeed_notices import JobfeedNotices
from pipeline.workday_accounts import AccountStore

API = 'https://discord.com/api/v10'
MAX_MESSAGE = 1900
# ✅ on a reply the bot acted on, 👀 on one it read but could not use (its text reply says why).
DONE_REACTION, SEEN_REACTION = '%E2%9C%85', '%F0%9F%91%80'
REVIEW_STEP, SUBMIT_WORD = 'ready for review', 'submit'
# A reply of one of these words to a question with a draft saves the draft as the user's answer. Any other reply asks for changes.
APPROVE_WORDS = frozenset({'approve', 'ok'})
DRAFT_FOOTER = 'Approve the draft, or press Request changes to say what should change. Write my own replaces the draft with your answer.'
QUESTION_FOOTER = 'Press the answer that fits. Other opens a box to type your own.'
OTHER = 'Other'
ENDED_STATUS = 'Closed: this job\'s Applier has stopped, so nothing is waiting for this answer.'
CHANGE_MESSAGE = ('After reading your review summary, the user asked for this change in Discord message {message_id}: "{text}". '
                  'Treat it as the user\'s answer. Call tabs_context_mcp and find your registered tab. Make only that change, '
                  'read the field back with record_readback, then call record_progress with "ready for review" and end with a one-line summary. Do not submit.')
OUTCOME_TEXT = {
    'submitted': 'Submitted. The confirmation page was saved.',
    'not_submitted': 'Not submitted. The form is still open in its tab, and a new summary follows once the Applier fixes it.',
    'unknown': 'Submit was clicked once, and the result is unclear. It will not be clicked again. Check the site or your email.',
}
TAB_CLOSED_TEXT = "Closed this job's browser tabs."
RECORDED_TEXT = 'Recorded in Jobfeed as applied.'
SHOT_TEXT = 'Confirmation page of {label}: <{url}>'
# Under Discord's 10 MiB upload limit for a server without boosts, so the upload never fails for its size.
SHOT_LIMIT = 8 * 1024 * 1024
CLOSED_TEXT = 'The posting is closed, so the Applier stopped. The job is marked unavailable, and Jobfeed marks it ignored. The page said: {reason}'
SKIPPED_TEXT = 'Skipped as you asked. The Applier stopped, the job is cancelled in the queue, and Jobfeed marks it ignored.'
AUTO_SUBMIT_TEXT = ('Auto submit is on, so Submit was approved for this summary. The Applier will click Submit once and report the result here. '
                    'To stop this for later jobs, run `pipeline.cli auto-submit off`.')
AUTO_BLOCKED_TEXT = 'Auto submit could not approve this summary: {reason} Check it and press Submit yourself.'
HOLD_TEXT = ('The job is set aside as CAPTCHA needed and stays in the queue. The batch goes on with other jobs. No Applier tries to solve it. '
             'Open the page in this computer\'s Chrome and complete the check yourself, then press I solved it, retry, '
             'or run `pipeline.cli captcha clear {job_id}`.')
RETRY_TEXT = {True: 'Retrying: {by} cleared the CAPTCHA hold. The Applier looks at its tab again.',
              False: 'Retrying: {by} cleared the CAPTCHA hold. The job starts again in its normal turn.'}
LOST_TAB_TEXT = ('The form tab was lost before Submit was clicked, so nothing was sent. The job went back to the queue, '
                 'and a new Applier fills the form again from the start in its normal turn. Nothing is needed from you.')
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS applier_discord_threads(job_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, last_seen TEXT);
CREATE TABLE IF NOT EXISTS applier_discord_posts(kind TEXT NOT NULL, ref TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(kind,ref));
CREATE TABLE IF NOT EXISTS applier_discord_cursors(source TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

# Discord message components and interaction callbacks.
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4
COMPONENT, MODAL_SUBMIT = 3, 5
REPLY, UPDATE, MODAL = 4, 7, 9
EPHEMERAL = 64
BLUE, RED, GREEN = 0x5865F2, 0xED4245, 0x57F287
# Discord limits: fields per embed, characters per field value, and a margin under the 6000 characters per message.
EMBED_FIELDS, FIELD_VALUE, MESSAGE_CHARS, MESSAGE_EMBEDS = 25, 1024, 5500, 10
THREAD_NAME = 100


def load_env(path):
    values = {}
    for line in Path(path).expanduser().read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def http_client(token):
    headers = {'Authorization': f'Bot {token}', 'User-Agent': 'DiscordBot (jobfeed-applier, 1.0)', 'Content-Type': 'application/json'}

    def call(method, path, body=None, file=None):
        """file is (filename, content type, bytes). It goes up as files[0], with the body as payload_json."""
        data, sent = None if body is None else json.dumps(body).encode(), headers
        if file is not None:
            name, media, content = file
            boundary = uuid.uuid4().hex
            payload = json.dumps(dict(body or {}, attachments=[{'id': 0, 'filename': name}])).encode()
            data = b''.join([
                f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode(), payload,
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="{name}"\r\nContent-Type: {media}\r\n\r\n'.encode(), content,
                f'\r\n--{boundary}--\r\n'.encode()])
            sent = dict(headers, **{'Content-Type': f'multipart/form-data; boundary={boundary}'})
        for _ in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(API + path, data=data, method=method, headers=sent), timeout=30) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise DiscordHTTPError(f'Discord {method} {path.split("?")[0]} failed with {exc.code}', exc.code) from None
                time.sleep(float(json.loads(exc.read() or b'{}').get('retry_after', 1)))
        raise RuntimeError('Discord kept rate limiting the bot')
    return call


class DiscordHTTPError(RuntimeError):
    """Discord refused a request. code is its HTTP status."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


# Discord refuses for good to edit a message in an archived thread, or one it no longer has.
REFUSED_FOR_GOOD = frozenset({400, 403, 404})


BLOCKER_GUIDANCE = {
    'tab_missing': 'The form tab is gone, so its draft is lost unless you still have it. Choose Reopen to start a fresh form, or Stop.',
    'browser_disconnected': 'Claude in Chrome lost its connection. Reconnect the extension, then press Done.',
    'already_applied': 'A run that lost its tab after Submit was started again, and the site shows an earlier application. Check it, then skip the job or press Done to apply anyway.',
    'page_changed': 'The form tab now shows a different page. Check it, then press Done or tell me what happened.',
    'foreign_owner': 'Another session controls this tab. Nothing was taken over. Reply with what to do.',
    'submit_unknown': 'Submit was clicked once and the result is unclear. It will not be clicked again. Check the site or your email, then press Submitted or Not submitted.',
}
# One-press replies for each blocker. Each saves exactly that word as the reply.
BLOCKER_WORDS = {'tab_missing': ('reopen', 'stop'), 'foreign_owner': (), 'submit_unknown': ('submitted', 'not submitted')}


def _clip(text):
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 20] + '\n… (shortened)'


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _button(label, custom_id, style=SECONDARY):
    return {'type': 2, 'style': style, 'label': label, 'custom_id': custom_id}


def _row(*buttons):
    return [{'type': 1, 'components': list(buttons)}]


def _notice(text, *buttons):
    data = {'content': text, 'flags': EPHEMERAL}
    if buttons:
        data['components'] = _row(*buttons)
    return {'type': REPLY, 'data': data}


def _modal(custom_id, title, label, *, value=None, placeholder=None):
    field = {'type': 4, 'custom_id': 'text', 'style': 2, 'label': _cut(label, 45), 'required': True, 'max_length': 4000}
    if value:
        field['value'] = value
    if placeholder:
        field['placeholder'] = _cut(placeholder, 100)
    return {'type': MODAL, 'data': {'custom_id': custom_id, 'title': _cut(title, 45), 'components': [{'type': 1, 'components': [field]}]}}


def _modal_text(interaction):
    return interaction['data']['components'][0]['components'][0]['value'].strip()


def _reply_status(reply):
    """What a settled question shows in place of its buttons."""
    if reply['intent'] == 'change_request':
        return _clip(f"Asked for changes: {reply['text'].rstrip('.')}. The Applier will send a new draft.")
    if reply.get('source_kind') == 'apply_browser':
        return 'Done automatically: the applying browser is connected again.'
    if reply.get('source_kind') == 'email':
        return 'Submitted: the employer\'s confirmation email arrived, so nobody needed to answer.'
    return _clip(f"Answered: {reply['text']}")


def _closed_status(store, request):
    """What a question that is no longer open shows in place of its buttons."""
    if request['state'] == 'withdrawn':
        return 'Closed: this job was skipped.'
    if request['state'] == 'retired':
        return 'Closed: this job went back to the queue. Its new Applier asks again if it needs this.'
    if request['state'] in {'escalated', 'escalation_sent'}:
        return 'Retrying on a stronger model. A new message follows if it still needs you.'
    return _reply_status(store.latest_reply(request['id']))


def _link(embed, url):
    if url.startswith(('https://', 'http://')):
        embed['url'] = url
    return embed


def question_message(request, url, label=None):
    """A question or blocker as one embed with buttons. A plain reply to it still answers it."""
    job, rid = request['job_id'], request['id']
    label = label or f'Job {job}'
    # Each of the form's fixed choices is a button that saves exactly that choice, five to a row.
    picks = [_button(option, f'pick:{rid}:{index}', PRIMARY) for index, option in enumerate(request.get('options') or [])]
    fields = [{'name': 'Form field', 'value': _cut(request['field'], FIELD_VALUE)}] if request.get('field') else []
    if request['kind'] == 'blocker':
        evidence = request.get('evidence') or {}
        if evidence.get('attempts'):
            fields.append({'name': 'Tried', 'value': _cut('; '.join(evidence['attempts']), FIELD_VALUE)})
        if evidence.get('error'):
            fields.append({'name': 'Error', 'value': _cut(evidence['error'], FIELD_VALUE)})
        guidance = BLOCKER_GUIDANCE.get(request['blocker_type'], 'Fix it in the browser and press Done, or reply with what to do.')
        embed = {'title': _cut(f"{label} · Blocked: {request['blocker_type'].replace('_', ' ')}", 256), 'color': RED,
                 'description': _cut(request['text'], 3500) + '\n\n' + guidance}
        buttons = [_button(word.capitalize(), f'quick:{rid}:{word}', PRIMARY) for word in BLOCKER_WORDS.get(request['blocker_type'], ('done',))]
        buttons.append(_button(OTHER, f'answer:{rid}'))
        content = f'**{label}** is blocked and needs you.'
    else:
        description = _cut(request['text'], 2500)
        if request.get('draft'):
            description += '\n\n**Draft answer**\n' + '\n'.join(f'> {line}' for line in request['draft'].splitlines())
            buttons = [_button('Approve draft', f'approve:{rid}', SUCCESS), _button('Request changes', f'revise:{rid}', PRIMARY),
                       _button('Write my own', f'answer:{rid}')]
        else:
            buttons = [_button(OTHER, f'answer:{rid}')]
        embed = {'title': _cut(f'{label} · Question', 256), 'color': BLUE, 'description': description}
        content = f'**{label}** has a question for you.'
    # Once Submit was clicked, the job can no longer be skipped.
    if request.get('blocker_type') != 'submit_unknown':
        buttons.append(_button('Ignore job', f'ignore:{job}', DANGER))
    embed.update(fields=fields, footer={'text': DRAFT_FOOTER if request.get('draft') else QUESTION_FOOTER})
    rows = [{'type': 1, 'components': picks[start:start + 5]} for start in range(0, len(picks), 5)]
    return {'content': content, 'embeds': [_link(embed, url)], 'components': rows + _row(*buttons)}


def hold_message(job_id, hold, url, label):
    """The notice that a job is set aside for a CAPTCHA, with the button that clears its hold."""
    description = _cut(hold.detail, 3000) + '\n\n' + HOLD_TEXT.format(job_id=job_id)
    embed = _link({'title': _cut(f'{label} · CAPTCHA needed', 256), 'color': RED, 'description': description,
                   'fields': [{'name': 'Page', 'value': _cut(hold.page_url, FIELD_VALUE)}] if hold.page_url else []}, hold.page_url or url)
    return {'content': f'**{label}** needs a CAPTCHA solved, so it is set aside.', 'embeds': [embed],
            'components': _row(_button('I solved it, retry', f'retry:{job_id}:{hold.id}', SUCCESS))}


def _size(embed):
    return (len(embed.get('title', '')) + len(embed.get('description', '')) + len((embed.get('footer') or {}).get('text', ''))
            + sum(len(f['name']) + len(f['value']) for f in embed.get('fields', [])))


def review_messages(job_id, url, fields, resume=None, ref=None, label=None, decided=(), accounts=()):
    """The review summary of a filled form as embeds, split across messages to fit Discord. The last message carries the buttons.

    decided holds the (question, answer) pairs the Applier answered itself under auto submit mode, listed last in full.
    accounts holds the Workday accounts the account program created for this job.
    """
    label = label or f'Job {job_id}'
    lines = [f'**{len(fields)}** answers are filled on the form. Check them, then press Submit, or Request a change.']
    for account in reversed(accounts):
        lines.insert(0, f"**Account:** created a Workday account on `{account['site']}` for {account['email']}")
    if resume is not None:
        lines.insert(0, f"**Resume:** {resume['label']}, uploaded as `{resume['upload_name']}`")
    if decided:
        lines.append(f'The Applier decided **{len(decided)}** answers itself under auto submit mode, without asking you. They are listed last, in full.')
    embeds = [_link({'title': _cut(f'{label} · Ready for review', 256), 'color': GREEN, 'description': '\n'.join(lines), 'fields': []}, url)]
    for field, value in [*fields, *((f'Applier decided · {question}', answer) for question, answer in decided)]:
        value = value or '*(left empty)*'
        if len(value) > FIELD_VALUE:
            # A long answer gets its own embed, so it shows whole instead of cut at the field limit.
            embeds.append({'title': _cut(field, 256), 'color': GREEN, 'description': _cut(value, 4000)})
            continue
        if 'fields' not in embeds[-1] or len(embeds[-1]['fields']) == EMBED_FIELDS:
            embeds.append({'color': GREEN, 'fields': []})
        embeds[-1]['fields'].append({'name': _cut(field, 256), 'value': value, 'inline': len(field) <= 30 and len(value) <= 30})
    embeds[-1]['footer'] = {'text': 'Submit sends this form once. Request a change to fix an answer first. Or reply submit to this message.'}
    messages, current = [], []
    for embed in embeds:
        if current and (len(current) == MESSAGE_EMBEDS or sum(map(_size, current)) + _size(embed) > MESSAGE_CHARS):
            messages.append({'embeds': current})
            current = []
        current.append(embed)
    messages.append({'embeds': current})
    messages[0]['content'] = f'**{label}** is ready for your review.'
    if ref is not None:
        messages[-1]['components'] = _row(_button('Submit', f'submit:{ref}', SUCCESS), _button('Request a change', f'change:{ref}', PRIMARY),
                                          _button('Ignore job', f'ignore:{job_id}', DANGER))
    return messages


class DiscordBridge:
    def __init__(self, root, *, http, channel_id, allowed_user_ids, job_urls=None, job_names=None, confirm_submit=None, confirm_auto=None, jobfeed_db=None,
                 job_evidence=None):
        self.store = ApplierRuntime(root)
        self.jobfeed_db = jobfeed_db
        self.http, self.channel_id = http, channel_id
        self.allowed = frozenset(allowed_user_ids)
        self.job_urls = job_urls or (lambda job_id: '')
        # job_names(job_id) is the company and title, so threads and messages say which job they are about.
        self.job_names = job_names or (lambda job_id: '')
        # confirm_submit(job_id, scope, message_id, sender_id) runs the Dashboard's Submit confirmation.
        self.confirm_submit = confirm_submit
        # confirm_auto(job_id, scope) runs the same confirmation under auto submit mode, or returns None while the job needs the user.
        self.confirm_auto = confirm_auto
        # job_evidence(job_id) is the job's submission evidence as Jobfeed validates it, or raises ValueError.
        self.job_evidence = job_evidence
        # With the Jobfeed database, Jobfeed activity is also announced in the channel.
        self.notices = JobfeedNotices(self.store.root, jobfeed_db, self._db) if jobfeed_db else None

    def _ended_jobs(self):
        """Jobs that had Applier launches and have none left running."""
        with closing(self._db()) as db:
            if 'state' not in {column['name'] for column in db.execute('PRAGMA table_info(applier_launches)')}:
                return set()
            rows = db.execute("SELECT job_id, MAX(state NOT IN ('stopped','finished','failed')) AS live FROM applier_launches GROUP BY job_id").fetchall()
        return {row['job_id'] for row in rows if not row['live']}

    def _label(self, job_id):
        name = self.job_names(job_id)
        return f'{name} (job {job_id})' if name else f'Job {job_id}'

    def _db(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.store.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.executescript(_SCHEMA)
        return db

    def _posted(self, kind, ref):
        with closing(self._db()) as db:
            row = db.execute('SELECT message_id FROM applier_discord_posts WHERE kind=? AND ref=?', (kind, ref)).fetchone()
        return None if row is None else row['message_id']

    def _posts(self, kind):
        with closing(self._db()) as db:
            return {row['ref']: row['message_id'] for row in db.execute('SELECT ref,message_id FROM applier_discord_posts WHERE kind=?', (kind,))}

    def _mark(self, kind, ref, message_id):
        with closing(self._db()) as db, db:
            db.execute('INSERT OR IGNORE INTO applier_discord_posts VALUES(?,?,?)', (kind, ref, message_id))

    def _is_review(self, message_id):
        with closing(self._db()) as db:
            return db.execute("SELECT 1 FROM applier_discord_posts WHERE kind IN ('review','review-part') AND message_id=?", (message_id,)).fetchone() is not None

    def _thread_id(self, job_id):
        with closing(self._db()) as db:
            row = db.execute('SELECT thread_id FROM applier_discord_threads WHERE job_id=?', (job_id,)).fetchone()
        return None if row is None else row['thread_id']

    def _post(self, kind, ref, channel, message, reply_to=None, file=None):
        """Send one message unless this kind and ref were already sent. message is text or a message body, and file an attachment."""
        if (existing := self._posted(kind, ref)) is not None:
            return existing
        body = dict({'content': message} if isinstance(message, str) else message, allowed_mentions={'parse': []})
        if reply_to:
            body['message_reference'] = {'message_id': reply_to, 'fail_if_not_exists': False}
        sent = self.http('POST', f'/channels/{channel}/messages', body, file)
        with closing(self._db()) as db, db:
            db.execute('INSERT OR IGNORE INTO applier_discord_posts VALUES(?,?,?)', (kind, ref, sent['id']))
        # A new message reopens an archived thread. A submitted job's thread goes back to the archive.
        if self._posted('archived', channel) is not None:
            self._archive(channel)
        return sent['id']

    def _archive(self, thread):
        self.http('PATCH', f'/channels/{thread}', {'archived': True})
        self._mark('archived', thread, thread)

    def _settle(self, kind, ref, channel, message_id, status):
        """Replace a message's buttons with the status of what happened, once."""
        if self._posted(kind, ref) is None:
            try:
                self.http('PATCH', f'/channels/{channel}/messages/{message_id}', {'content': _clip(status), 'components': []})
            except DiscordHTTPError as exc:
                # The edit only shows the outcome in place of the buttons. A refused one is not tried on every pass (2026-11-18:
                # a nine-day-old question in an archived thread failed with 400 on each pass and ended the rest of it).
                if exc.code not in REFUSED_FOR_GOOD:
                    raise
            self._mark(kind, ref, message_id)

    def thread_for(self, job_id):
        """The Discord thread for one job. Job 0 is the Fixer's thread."""
        with closing(self._db()) as db:
            row = db.execute('SELECT thread_id FROM applier_discord_threads WHERE job_id=?', (job_id,)).fetchone()
        if row is not None:
            return row['thread_id']
        title = 'Fixer' if job_id == 0 else self._label(job_id)
        intro = '**Fixer** summaries: helpers added, skipped items, and failed runs.' if job_id == 0 else f'**{title}** {self.job_urls(job_id)}'.strip()
        starter = self._post('job', str(job_id), self.channel_id, _clip(intro))
        thread = self.http('POST', f'/channels/{self.channel_id}/messages/{starter}/threads', {'name': _cut(title, THREAD_NAME), 'auto_archive_duration': 10080})
        with closing(self._db()) as db, db:
            db.execute('INSERT OR IGNORE INTO applier_discord_threads(job_id,thread_id) VALUES(?,?)', (job_id, thread['id']))
        if title != f'Job {job_id}':
            self._mark('thread-named', str(job_id), thread['id'])
        return self.thread_for(job_id)

    def _name_threads(self):
        """Rename each thread made before it carried the job's name, once the name is known."""
        named = self._posts('thread-named')
        with closing(self._db()) as db:
            rows = db.execute('SELECT job_id,thread_id FROM applier_discord_threads WHERE job_id != 0').fetchall()
        for row in rows:
            if str(row['job_id']) in named or (title := self._label(row['job_id'])) == f"Job {row['job_id']}":
                continue
            try:
                self.http('PATCH', f"/channels/{row['thread_id']}", {'name': _cut(title, THREAD_NAME)})
            except RuntimeError:
                pass  # Discord refuses to rename a thread that auto-archived or was deleted. The name is only a convenience.
            self._mark('thread-named', str(row['job_id']), row['thread_id'])

    def _archive_submitted(self, jobs):
        """Archive the thread of each job in jobs once, after its last outcome is posted."""
        for job_id in jobs:
            if (thread := self._thread_id(job_id)) is not None and self._posted('archived', thread) is None:
                self._archive(thread)

    def sync(self):
        """Post new requests, alerts, readbacks and review summaries, act on replies from the allowed account, settle old buttons,
        then announce new Jobfeed activity in the channel."""
        events = []
        self._name_threads()
        # Read before posting: a job submitted later in this pass has its buttons settled first, in the next pass.
        submitted = [job['job_id'] for job in self.store.jobs() if (self.store.submit_state(job['job_id']) or {}).get('state') == 'submitted']
        # Confirm filled answers before asking anything new, so the newest question is the last message.
        for readback in self.store.reply_readbacks():
            if self._posted('readback', readback['id']) is None:
                question = self._posted('request', readback['request_id'])
                self._post('readback', readback['id'], self.thread_for(readback['job_id']),
                           _clip(f"Filled {readback['field']}: {readback['value'] or '(left empty)'} (reply version {readback['reply_version']})"), reply_to=question)
        # A CAPTCHA report needs no answer: its job's hold notice carries the button. Nor does a tab lost before any Submit
        # attempt: the worker puts its job back in the queue, and the notice of that follows.
        lost = {report['id'] for report in self.store.lost_tabs()}
        open_requests = [r for r in self.store.requests() if r['state'] == 'open' and r['blocker_type'] != 'captcha' and r['id'] not in lost]
        # Every question and blocker is asked in the main channel, so the user answers them all in one place.
        # One asked earlier in its job's thread is asked there again; answering either closes both. A job whose
        # every Applier has ended has nobody to take the answer, so its leftover questions stay where they were.
        ended = self._ended_jobs()
        for request in open_requests:
            if self._posted('ask', request['id']) is None and int(request['job_id']) not in ended:
                full = self.store.request(request['id'])
                # Whether Submit went through is read from the employer's email first; the user hears of it only if none came.
                if full['blocker_type'] == 'submit_unknown' and not ask_user_now(full):
                    continue
                self._post('ask', request['id'], self.channel_id, question_message(full, self.job_urls(full['job_id']), self._label(full['job_id'])))
                events.append({'event': 'posted', 'request_id': request['id']})
        for alert in self.store.alerts(unsent=True):
            ref = f"{alert['job_id']}:{alert['kind']}:{alert['detail']}"
            self._post('alert', ref, self.thread_for(alert['job_id']), _clip(f"Watchdog: {alert['kind'].replace('_', ' ')}. {alert['detail']}"))
            self.store.alert_sent(alert['job_id'], alert['kind'], alert['detail'])
            events.append({'event': 'alert_posted', 'job_id': alert['job_id'], 'kind': alert['kind']})
        events += self._post_holds()
        events += self._post_lost_tabs()
        fixer = Fixer(self.store.root)
        for run in fixer.unposted():
            self._post('fix', run['id'], self.thread_for(0), _clip(run['summary'] or f"Fixer run {run['state']}."))
            fixer.mark_posted(run['id'])
            events.append({'event': 'fix_posted', 'run_id': run['id']})
        reviews = self._post_reviews()
        events += self._post_outcomes()
        for job_id in sorted({r['job_id'] for r in open_requests} | reviews.keys()):
            events += self._collect(job_id, [r for r in open_requests if r['job_id'] == job_id], reviews.get(job_id))
        if self.confirm_auto is not None:
            for job_id, review in reviews.items():
                events += self._auto_submit(job_id, review)
        self._settle_requests()
        self._settle_reviews(reviews)
        self._archive_submitted(submitted)
        if self.notices is not None:
            events += self.notices.post(lambda ref, text: self._post('notice', ref, self.channel_id, _clip(text)))
        return events

    def _current_reviews(self):
        """Each job whose draft is ready for review, keyed to a ref that changes with every version of its filled form.

        Any new readback makes a new version, so Submit always refers to the form as it now stands.
        """
        reviews = {}
        for job in self.store.jobs():
            submit = self.store.submit_state(job['job_id'])
            if job['last_progress'] != REVIEW_STEP or (submit is not None and submit['state'] not in RETRYABLE_SUBMIT) or self.store.skip(job['job_id']):
                continue
            # After an attempt that sent nothing, the Applier fixes the form and says it is ready again. That retry gets
            # its own summary and approval, even when the fix recorded no new readback.
            if submit is not None and job['last_progress_at'] < submit['outcome_at']:
                continue
            fields, last_readback = self.store.filled_form(job['job_id'])
            resume = self.store.resume(job['job_id'])
            version = [job['owner'], last_readback, fields, resume] + ([submit['attempts']] if submit else [])
            digest = hashlib.sha256(json.dumps(version).encode()).hexdigest()[:16]
            reviews[job['job_id']] = {'ref': f"{job['job_id']}:{digest}", 'scope': job['scope'], 'fields': fields, 'resume': resume,
                                      'decided': self._decided(job['job_id']), 'accounts': AccountStore(self.store.root).created_by(job['job_id'])}
        return reviews

    def _decided(self, job_id):
        try:
            entries = AnswerBank(self.store.root).read()['entries']
        except BankError:
            return []
        return [(e['versions'][-1]['question'], e['versions'][-1]['answer']) for e in entries if applier_decided(e['versions'][-1], job_id)]

    def _post_reviews(self):
        """Post the summary of each draft that is ready for review, once per version of its filled form."""
        reviews = {}
        for job_id, review in self._current_reviews().items():
            ref, thread = review['ref'], self.thread_for(job_id)
            *parts, last = review_messages(job_id, self.job_urls(job_id), review['fields'], review['resume'], ref, self._label(job_id), review['decided'],
                                           review['accounts'])
            for index, part in enumerate(parts):
                self._post('review-part', f'{ref}:{index}', thread, part)
            reviews[job_id] = {'ref': ref, 'message': self._post('review', ref, thread, last), 'thread': thread, 'scope': review['scope']}
        return reviews

    def _post_holds(self):
        """Post each new CAPTCHA hold in its job's thread, and settle the notice once the hold is cleared."""
        if self.jobfeed_db is None:
            return []
        from pipeline.store import ApplicationQueue
        events = []
        for job_id, owner, hold in ApplicationQueue(self.jobfeed_db).holds():
            ref = f'{job_id}:{hold.id}'
            posted = self._posted('hold', ref)
            if hold.active and posted is None:
                self._post('hold', ref, self.thread_for(job_id), hold_message(job_id, hold, self.job_urls(job_id), self._label(job_id)))
                events.append({'event': 'hold_posted', 'job_id': job_id})
            elif not hold.active and posted is not None:
                self._settle('hold-settled', ref, self.thread_for(job_id), posted, RETRY_TEXT[bool(owner)].format(by=hold.cleared_by))
        return events

    def _post_lost_tabs(self):
        """Tell each job's thread once that the worker put the job back in the queue because its form tab was lost."""
        from pipeline.captcha_holds import LOST_TAB_WORKER
        events = []
        for entry in self.store.requeued_by(LOST_TAB_WORKER):
            ref = f"{entry['job_id']}:{entry['id']}"
            if self._posted('lost-tab', ref) is None:
                self._post('lost-tab', ref, self.thread_for(entry['job_id']), LOST_TAB_TEXT)
                events.append({'event': 'lost_tab_posted', 'job_id': entry['job_id']})
        return events

    def _post_outcomes(self):
        events = []
        for row in self.store.outbox():
            if row['state'] != 'done':
                continue
            # A job asked about only in the main channel has no thread yet, and the Ignore button promised a note in one.
            if row['kind'] == 'user_skip':
                self._post('outcome', f"{row['job_id']}:skipped", self.thread_for(row['job_id']), SKIPPED_TEXT)
                continue
            if (thread := self._thread_id(row['job_id'])) is None:
                continue
            if row['kind'] == 'posting_closed':
                self._post('outcome', f"{row['job_id']}:closed", thread, _clip(CLOSED_TEXT.format(reason=self.store.skip(row['job_id'])['reason'])))
            elif row['kind'] == 'jobfeed_sync':
                self._post('outcome', f"{row['job_id']}:recorded", thread, RECORDED_TEXT)
                events += self._post_shot(row['job_id'], thread)
        for job in self.store.jobs():
            state, thread = self.store.submit_state(job['job_id']), self._thread_id(job['job_id'])
            if thread is None or state is None:
                continue
            if state['state'] in OUTCOME_TEXT:
                text = OUTCOME_TEXT[state['state']] + (f"\nThe Applier saw: {state['evidence']}" if state['state'] != 'submitted' and state['evidence'] else '')
                self._post('outcome', f"{job['job_id']}:{state['attempts']}:{state['state']}", thread, _clip(text))
            if state['state'] == 'submitted' and self.store.tabs_closed(job['job_id']):
                self._post('outcome', f"{job['job_id']}:tab-closed", thread, TAB_CLOSED_TEXT)
        return events

    def _post_shot(self, job_id, thread):
        """Post the confirmation screenshot of a job recorded in Jobfeed in its thread, once. An email confirmation has none."""
        ref = str(job_id)
        if self.job_evidence is None or self._posted('evidence-shot', ref) is not None:
            return []
        try:
            evidence = self.job_evidence(job_id)
        except ValueError as exc:
            skipped = str(exc)
        else:
            if evidence.kind != 'website_screenshot':
                self._mark('evidence-shot', ref, '')
                return []
            skipped = 'screenshot_too_large' if len(evidence.artifact_bytes) > SHOT_LIMIT else None
        if skipped:
            self._mark('evidence-shot', ref, '')
            return [{'event': 'evidence_shot_skipped', 'job_id': job_id, 'reason': skipped}]
        name = 'confirmation.png' if evidence.media_type == 'image/png' else 'confirmation.jpg'
        self._post('evidence-shot', ref, thread, _clip(SHOT_TEXT.format(label=self._label(job_id), url=evidence.source_url)),
                   file=(name, evidence.media_type, evidence.artifact_bytes))
        return [{'event': 'evidence_shot_posted', 'job_id': job_id}]

    def _settle_requests(self):
        """Take the buttons off each question that is no longer open, in the main channel and in an older thread post.
        In the main channel, also off a question whose job has no Applier left to take the answer."""
        ended = self._ended_jobs()
        for posted, settled_kind, in_main in (('ask', 'ask-settled', True), ('request-styled', 'request-settled', False)):
            settled = self._posts(settled_kind)
            for ref, message_id in self._posts(posted).items():
                if ref in settled:
                    continue
                request = self.store.request(ref)
                if request['state'] == 'open' and not (in_main and int(request['job_id']) in ended):
                    continue
                status = _closed_status(self.store, request) if request['state'] != 'open' else ENDED_STATUS
                # The main channel mixes every job, so its settled text names the job; a thread already does.
                channel = self.channel_id if in_main else self.thread_for(request['job_id'])
                self._settle(settled_kind, ref, channel, message_id, f"{self._label(request['job_id'])}: {status}" if in_main else status)

    def _settle_reviews(self, reviews):
        """Take the buttons off each summary that is no longer the one to act on."""
        current = {review['message'] for review in reviews.values()}
        closed, settled = self._posts('review-closed'), self._posts('review-settled')
        for ref, message_id in self._posts('review').items():
            if ref in settled or (message_id in current and ref not in closed):
                continue
            job_id = int(ref.split(':')[0])
            submit = self.store.submit_state(job_id)
            if self.store.skip(job_id):
                status = 'Ignored: this job was skipped.'
            elif submit is not None and submit['state'] not in RETRYABLE_SUBMIT:
                status = 'Submit approved for this summary.'
            elif ref in closed:
                status = 'Change requested. A new summary follows once the Applier makes it.'
            else:
                status = 'Out of date. Use the newest summary below.'
            self._settle('review-settled', ref, self.thread_for(job_id), message_id, status)

    def _submit(self, job_id, review, source_id, sender_id):
        """Run the Submit confirmation for the summary the user acted on. Returns the answer to show and the event."""
        if self._posted('review-closed', review['ref']) is not None:
            return 'A change you asked for is still being made. Submit the new summary.', 'submit_waiting'
        if self.confirm_submit is None:
            return 'Submit from Discord is not set up here. Use Submit application in Runs.', 'submit_unavailable'
        try:
            result = self.confirm_submit(job_id, review['scope'], source_id, sender_id)
        except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {'state': 'blocked', 'reason': str(exc)}
        if result['state'] == 'blocked':
            return f"Not submitted. {result['reason']}", 'submit_refused'
        return ' '.join(filter(None, ['Submit approved. The Applier will click Submit once and report the result here.', result.get('reason')])), 'submit_confirmed'

    def _auto_submit(self, job_id, review):
        """Run the auto confirmation for the newest summary, and post its result in the thread once."""
        if self._posted('review-closed', review['ref']) is not None:
            return []
        try:
            result = self.confirm_auto(job_id, review['scope'])
        except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
            return [{'event': 'auto_submit_unavailable', 'job_id': job_id, 'error': str(exc)[:200]}]
        if result is None or self._posted('auto-submit', result['request_id']) is not None:
            return []
        confirmed = result['state'] != 'blocked'
        text = ' '.join(filter(None, [AUTO_SUBMIT_TEXT, result.get('reason')])) if confirmed else AUTO_BLOCKED_TEXT.format(reason=result['reason'])
        self._post('auto-submit', result['request_id'], review['thread'], _clip(text), reply_to=review['message'])
        return [{'event': 'auto_submit_confirmed' if confirmed else 'auto_submit_blocked', 'job_id': job_id}]

    def _request_change(self, job_id, review, text, source_id):
        self.store.queue_message(job_id, CHANGE_MESSAGE.format(message_id=source_id, text=text), f'discord-change-{source_id}')
        # Submit waits for the summary the Applier posts after the change.
        self._mark('review-closed', review['ref'], source_id)

    def _review_reply(self, job_id, review, message):
        text, thread = message['content'].strip(), review['thread']
        if text.casefold() != SUBMIT_WORD:
            self._request_change(job_id, review, text, message['id'])
            self._post('change', message['id'], thread, 'Sent to the Applier. It will make the change and post a new summary.', reply_to=message['id'])
            return {'event': 'change_requested', 'job_id': job_id}
        answer, event = self._submit(job_id, review, message['id'], message['author']['id'])
        self._post('submit', message['id'], thread, _clip(answer), reply_to=message['id'])
        return {'event': event, 'job_id': job_id}

    def _collect(self, job_id, requests, review=None):
        with closing(self._db()) as db:
            row = db.execute('SELECT thread_id,last_seen FROM applier_discord_threads WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            return []
        query = f"?limit=50&after={row['last_seen']}" if row['last_seen'] else '?limit=50'
        messages = sorted(self.http('GET', f"/channels/{row['thread_id']}/messages{query}"), key=lambda m: int(m['id']))
        # Only questions asked in the thread by an older version have a message here to reply to.
        posts = {message_id: r['id'] for r in requests if (message_id := self._posted('request', r['id'])) is not None}
        closed = set(self._posts('request').values()) - set(posts)
        events = []
        for message in messages:
            author = message.get('author') or {}
            if author.get('bot') or author.get('id') not in self.allowed or not (message.get('content') or '').strip():
                continue
            reference = (message.get('message_reference') or {}).get('message_id')
            if review is not None and reference == review['message']:
                events.append(self._review_reply(job_id, review, message))
                self._react(row['thread_id'], message['id'], DONE_REACTION)
                continue
            if reference is not None and self._is_review(reference):
                self._post('stale', message['id'], row['thread_id'], 'That summary is out of date. Use the newest one.', reply_to=message['id'])
                self._react(row['thread_id'], message['id'], SEEN_REACTION)
                continue
            if reference in closed:
                self._post('stale', message['id'], row['thread_id'], 'That question is closed. Answer the newest open question.', reply_to=message['id'])
                self._react(row['thread_id'], message['id'], SEEN_REACTION)
                continue
            target = posts.get(reference)
            if target is None and len(requests) == 1:
                target = requests[0]['id']
            if target is None:
                hint = ('To submit, press Submit on the review summary.' if not requests
                        else 'Several questions are open for this job. Use the buttons on the one you are answering, in the main channel.')
                self._post('ambiguous', message['id'], row['thread_id'], hint, reply_to=message['id'])
                self._react(row['thread_id'], message['id'], SEEN_REACTION)
                continue
            text, draft, intent = message['content'].strip(), self.store.request(target).get('draft'), 'answer'
            if draft and text.casefold() in APPROVE_WORDS:
                text = draft
            elif draft:
                intent = 'change_request'
            version = self.store.reply(target, text, source_kind='discord', source_id=message['id'], sender_id=author['id'], intent=intent)
            self._react(row['thread_id'], message['id'], DONE_REACTION)
            events.append({'event': 'reply_saved', 'request_id': target, 'version': version})
        if messages:
            with closing(self._db()) as db, db:
                db.execute('UPDATE applier_discord_threads SET last_seen=? WHERE job_id=?', (messages[-1]['id'], job_id))
        return events

    def _react(self, channel, message_id, emoji):
        self.http('PUT', f'/channels/{channel}/messages/{message_id}/reactions/{emoji}/@me')

    def interact(self, interaction):
        """Handle one button press or form from the gateway. Discord gets its answer first, then any slow work runs.

        custom_id is action:ref. Only the allowed account can act, and Submit and Ignore each need a second press.
        """
        if interaction.get('type') not in (COMPONENT, MODAL_SUBMIT):
            return None
        user = (interaction.get('member') or {}).get('user') or interaction.get('user') or {}
        action, _, ref = interaction['data']['custom_id'].partition(':')
        handler = {'answer': self._on_answer, 'approve': self._on_approve, 'revise': self._on_revise, 'quick': self._on_quick, 'pick': self._on_pick, 'submit': self._on_submit,
                   'confirm': self._on_confirm, 'change': self._on_change, 'ignore': self._on_ignore, 'ignored': self._on_ignored,
                   'retry': self._on_retry}.get(action)
        later = None
        if user.get('id') not in self.allowed:
            body = _notice('Only the account that runs these applications can use these buttons.')
        elif handler is None:
            body = _notice('This button is from an older version. Use the newest message.')
        else:
            try:
                body, later = handler(interaction, ref, user['id'])
            except ValueError as exc:
                body = _notice(str(exc))
        self.http('POST', f"/interactions/{interaction['id']}/{interaction['token']}/callback", body)
        if later is not None:
            later()
        return {'event': 'interaction', 'action': action, 'ref': ref}

    def _open_request(self, request_id):
        request = self.store.request(request_id)
        if request['state'] != 'open':
            raise ValueError('This question is already closed.')
        return request

    def _answered(self, interaction, request, text, sender, intent='answer'):
        self.store.reply(request['id'], text, source_kind='discord', source_id=interaction['id'], sender_id=sender, intent=intent)
        status = _reply_status({'text': text, 'intent': intent})
        if (message := interaction.get('message')) is None:
            return _notice(status), None
        asked_here = self._posted('ask', request['id']) == message['id']
        self._mark('ask-settled' if asked_here else 'request-settled', request['id'], message['id'])
        if asked_here:
            status = f"{self._label(request['job_id'])}: {status}"
        return {'type': UPDATE, 'data': {'content': _clip(status), 'components': []}}, None

    def _on_answer(self, interaction, ref, sender):
        request = self._open_request(ref)
        if interaction['type'] == COMPONENT:
            return _modal(f'answer:{ref}', f"Job {request['job_id']} · your answer", request.get('field') or 'Your answer',
                          value=request.get('draft'), placeholder=request['text']), None
        return self._answered(interaction, request, _modal_text(interaction), sender)

    def _on_revise(self, interaction, ref, sender):
        request = self._open_request(ref)
        if not request.get('draft'):
            raise ValueError('This question has no draft. Press Other.')
        if interaction['type'] == COMPONENT:
            return _modal(f'revise:{ref}', f"Job {request['job_id']} · request changes", 'What should change?',
                          placeholder='For example: shorter, and name the payments project'), None
        return self._answered(interaction, request, _modal_text(interaction), sender, intent='change_request')

    def _on_approve(self, interaction, ref, sender):
        request = self._open_request(ref)
        if not request.get('draft'):
            raise ValueError('This question has no draft. Press Other.')
        return self._answered(interaction, request, request['draft'], sender)

    def _on_quick(self, interaction, ref, sender):
        request_id, _, word = ref.partition(':')
        request = self._open_request(request_id)
        if word not in BLOCKER_WORDS.get(request['blocker_type'], ('done',)):
            raise ValueError('That reply does not fit this blocker.')
        return self._answered(interaction, request, word, sender)

    def _on_pick(self, interaction, ref, sender):
        request_id, _, index = ref.partition(':')
        request = self._open_request(request_id)
        options = request.get('options') or []
        if not index.isdigit() or int(index) >= len(options):
            raise ValueError('That choice does not fit this question.')
        return self._answered(interaction, request, options[int(index)], sender)

    def _review(self, ref):
        job_id = int(ref.split(':')[0])
        review = self._current_reviews().get(job_id)
        if review is None or review['ref'] != ref or (message := self._posted('review', ref)) is None:
            raise ValueError('That summary is out of date. Use the newest one.')
        return job_id, dict(review, message=message, thread=self.thread_for(job_id))

    def _on_submit(self, interaction, ref, sender):
        job_id, review = self._review(ref)
        if self._posted('review-closed', ref) is not None:
            raise ValueError('A change you asked for is still being made. Submit the new summary.')
        return _notice(f'Submit job {job_id} once, with exactly the answers in this summary?', _button('Yes, submit once', f'confirm:{ref}', SUCCESS)), None

    def _on_confirm(self, interaction, ref, sender):
        job_id, review = self._review(ref)
        token = f"/webhooks/{interaction['application_id']}/{interaction['token']}/messages/@original"

        def confirm():
            answer, _ = self._submit(job_id, review, interaction['id'], sender)
            self.http('PATCH', token, {'content': _clip(answer), 'components': []})
            self._post('submit', interaction['id'], review['thread'], _clip(answer), reply_to=review['message'])
        return {'type': UPDATE, 'data': {'content': 'Confirming Submit…', 'components': []}}, confirm

    def _on_change(self, interaction, ref, sender):
        job_id, review = self._review(ref)
        if interaction['type'] == COMPONENT:
            return _modal(f'change:{ref}', f'Job {job_id} · request a change', 'What should change?',
                          placeholder='For example: Sponsorship should be No'), None
        text = _modal_text(interaction)
        if not text:
            raise ValueError('Describe the change.')
        self._request_change(job_id, review, text, interaction['id'])
        status = _clip(f'Change requested: {text}\nThe Applier will make it and post a new summary.')
        self._mark('review-settled', ref, review['message'])
        return {'type': UPDATE, 'data': {'content': status, 'components': []}}, None

    def _on_ignore(self, interaction, ref, sender):
        job_id = int(ref)
        if self.store.skip(job_id):
            raise ValueError(f'Job {job_id} is already skipped.')
        return _notice(f'Ignore job {job_id}? Its Applier stops, the job is cancelled in the queue, and Jobfeed marks it ignored.',
                       _button(f'Yes, ignore job {job_id}', f'ignored:{job_id}', DANGER)), None

    def _on_ignored(self, interaction, ref, sender):
        job_id = int(ref)
        self.store.ignore(job_id, source_id=interaction['id'], sender_id=sender)
        return {'type': UPDATE, 'data': {'content': f'Ignoring job {job_id}. A note follows in its thread when it is done.', 'components': []}}, None

    def _on_retry(self, interaction, ref, sender):
        from pipeline.captcha_holds import clear
        job_id, _, hold_id = ref.partition(':')
        item = clear(self.store.root, self.jobfeed_db, int(job_id), f'discord:{sender}', hold_id)
        if item is None:
            raise ValueError(f'Job {job_id} is no longer set aside for this CAPTCHA.')
        self._mark('hold-settled', f'{job_id}:{hold_id}', interaction['message']['id'])
        return {'type': UPDATE, 'data': {'content': RETRY_TEXT[bool(item.codex_application_session_id)].format(by='You'), 'components': []}}, None


DEFAULT_ENV = Path('~/.config/jobfeed-applier/discord.env')


def discord_confirmer(config):
    from pipeline.dashboard_submission import DashboardSubmissions

    def confirm(job_id, scope, message_id, sender_id):
        return DashboardSubmissions(config).confirm_discord(job_id=job_id, scope=scope, message_id=message_id, sender_id=sender_id)
    return confirm


def auto_confirmer(config):
    from pipeline.dashboard_submission import DashboardSubmissions

    def confirm(job_id, scope):
        return DashboardSubmissions(config).confirm_auto(job_id=job_id, scope=scope)
    return confirm


def confirmation_reader(config):
    """Read a job's submission evidence from its own application folder, checked as Jobfeed checks it when it records the job."""
    from jobfeed.submission_evidence import read_confirmation
    from pipeline.store import ApplicationQueue

    def read(job_id):
        item = ApplicationQueue(config.jobfeed_db).get(job_id)
        with closing(sqlite3.connect(config.jobfeed_db.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT url,company FROM jobs WHERE id=?', (job_id,)).fetchone()
        folder = config.applications_root / item.application_folder_relpath if item and item.application_folder_relpath else None
        if folder is None:
            # A submitted job leaves the queue; its Applier still runs in a folder inside the job's application folder.
            launch = ApplierRuntime(config.resume_root)._read('SELECT config FROM applier_launches WHERE job_id=?', (job_id,))
            folder = Path(json.loads(launch[0]['config'])['cwd']).parent if launch else None
        if folder is None or row is None or not folder.resolve().is_relative_to(config.applications_root.resolve()):
            raise ValueError('no_application_folder')
        return read_confirmation(folder, job_id, row[0], company=row[1])
    return read


def main_channel(env, http):
    """The bridge's channel and the sender IDs it accepts: DISCORD_CHANNEL_ID or the server's first text channel,
    and DISCORD_ALLOWED_USER_IDS or the server owner."""
    guild = http('GET', f"/guilds/{env['DISCORD_GUILD_ID']}")
    channel = env.get('DISCORD_CHANNEL_ID') or next(c['id'] for c in http('GET', f"/guilds/{guild['id']}/channels") if c['type'] == 0)
    allowed = [u for u in env.get('DISCORD_ALLOWED_USER_IDS', '').split(',') if u] or [guild['owner_id']]
    return channel, allowed


def bridge_from_env(root, env_path=DEFAULT_ENV, config=None, *, listen=False):
    """Build the bridge from the local token file. The allowed sender defaults to the server owner.

    listen starts the gateway listener, which button presses need.
    """
    from pipeline.config import PipelineConfig
    env = load_env(env_path)
    http = http_client(env['DISCORD_BOT_TOKEN'])
    channel, allowed = main_channel(env, http)

    def job_urls(job_id):
        with closing(sqlite3.connect(ApplierRuntime(root).path)) as db:
            try:
                row = db.execute('SELECT config FROM applier_launches WHERE job_id=?', (job_id,)).fetchone()
            except sqlite3.OperationalError:
                row = None
        return json.loads(row[0]).get('url', '') if row else ''

    config = config or PipelineConfig.discover(root)

    def job_names(job_id):
        try:
            with closing(sqlite3.connect(config.jobfeed_db.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT company,title FROM jobs WHERE id=?', (job_id,)).fetchone()
        except sqlite3.Error:
            row = None
        return ' · '.join(part for part in row if part) if row else ''

    bridge = DiscordBridge(root, http=http, channel_id=channel, allowed_user_ids=allowed, job_urls=job_urls, job_names=job_names,
                           confirm_submit=discord_confirmer(config), confirm_auto=auto_confirmer(config), jobfeed_db=config.jobfeed_db,
                           job_evidence=confirmation_reader(config))
    if listen:
        from pipeline.discord_gateway import Gateway
        Gateway(env['DISCORD_BOT_TOKEN'], bridge.interact).start()
    return bridge


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--env', type=Path, default=DEFAULT_ENV)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    bridge = bridge_from_env(args.root, args.env, listen=not args.once)
    while True:
        try:
            for event in bridge.sync():
                print(json.dumps(event), flush=True)
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            print(json.dumps({'event': 'error', 'error': str(exc), 'at': datetime.now(timezone.utc).isoformat()}), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
