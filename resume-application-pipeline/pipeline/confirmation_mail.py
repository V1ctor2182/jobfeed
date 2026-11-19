"""Answer an Applier's "did Submit go through?" from the employer's confirmation email, so the user is not asked.

For each open submit_unknown blocker, the program searches Gmail for the company's mail since the blocker was raised and
hands each message's raw bytes to confirmation_email.record, which keeps one only when Jobfeed's own evidence check
accepts it. The blocker is then answered "submitted" from source email, with the message as its provenance, and the
Applier records the outcome against the saved email. When no such email has arrived after SETTLE_AFTER, the user's
standing rule answers it: no confirmation email means not submitted. The user is asked only when this check has not
run for ASK_AFTER. Gmail is reached as the account program reaches it: through `claude -p`, reading only the raw tool result.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib

SEARCH = 'mcp__claude_ai_Gmail__search_threads'
MESSAGE = 'mcp__claude_ai_Gmail__get_message'
CAPTURE_TOOL = 'claude.ai Gmail connector get_message RAW'
# Employers usually confirm within minutes. After this long with no email, the application counts as not submitted.
SETTLE_AFTER = timedelta(hours=2)
NO_EMAIL = 'no_email'
# Only when this check is switched off or cannot reach Gmail does the question reach the user.
ASK_AFTER = timedelta(hours=12)
MAX_MESSAGES = 5


class GmailError(RuntimeError):
    pass


def _call(tool, tool_input):
    from pipeline import workday_accounts
    workday_accounts._PROMPT_ARGUMENTS.setdefault(MESSAGE, '{"messageId":"x"}')
    try:
        return workday_accounts.gmail_call(tool, tool_input)
    except workday_accounts.GmailError as exc:
        raise GmailError(str(exc)) from None


def waiting(store):
    """The open submit_unknown blockers of jobs with a running Applier, oldest first. An ended Applier cannot take the answer."""
    live = {int(row['job_id']) for row in store._read("SELECT DISTINCT job_id FROM applier_launches WHERE state NOT IN ('stopped','finished','failed')", ())}
    rows = [store.request(r['id']) for r in store.requests()
            if r['state'] == 'open' and r['blocker_type'] == 'submit_unknown' and int(r['job_id']) in live]
    return sorted(rows, key=lambda r: r['created_at'])


def ask_user_now(request, now=None):
    """Whether a submit_unknown blocker has waited long enough for an email that the user must now be asked."""
    created = datetime.fromisoformat(request['created_at'])
    return (now or datetime.now(timezone.utc)) - created >= ASK_AFTER


def _messages(company, since, call):
    query = f'"{company}" after:{(since - timedelta(days=1)).strftime("%Y/%m/%d")}'
    found = call(SEARCH, {'query': query, 'pageSize': 10, 'view': 'THREAD_VIEW_METADATA_ONLY'})
    messages = [m for thread in found.get('threads') or () for m in thread.get('messages') or ()]
    return sorted(messages, key=lambda m: int(m.get('internalDate') or 0), reverse=True)[:MAX_MESSAGES]


def check(pipeline, store, *, call=_call, record=None, tried=None, now=None):
    """Try each waiting job's recent mail. Returns one event per job answered from an email.

    tried holds (request id, message id) pairs already rejected, so a later check fetches only new mail. A job whose
    mailbox was read and that has waited SETTLE_AFTER with no confirmation is answered "not submitted".
    """
    if record is None:
        from pipeline.confirmation_email import record
    tried = set() if tried is None else tried
    events = []
    for request in waiting(store):
        job_id = int(request['job_id'])
        job = pipeline.jobfeed.get_job(job_id)
        if job is None or not job.company:
            continue
        try:
            messages = _messages(job.company, datetime.fromisoformat(request['created_at']), call)
        except GmailError:
            continue
        for message in messages:
            if (request['id'], message['id']) in tried:
                continue
            tried.add((request['id'], message['id']))
            try:
                raw = call(MESSAGE, {'messageId': message['id'], 'messageFormat': 'RAW'}).get('raw')
            except GmailError:
                continue  # A message too large for the courier is never a short confirmation.
            if not raw:
                continue
            data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
            try:
                result = record(pipeline, job_id, data, source_url=message.get('viewUrl') or f"gmail:{message['id']}", capture_tool=CAPTURE_TOOL)
            except ValueError:
                continue
            if result['status'] != 'confirmed':
                continue
            source = 'email-' + hashlib.sha256(result['message_id'].encode()).hexdigest()[:32]
            store.reply(request['id'], 'submitted', source_kind='email', source_id=source, sender_id='employer-confirmation-email')
            events.append({'event': 'submit_confirmed_by_email', 'job_id': job_id, 'message_id': result['message_id']})
            break
        else:
            if (now or datetime.now(timezone.utc)) - datetime.fromisoformat(request['created_at']) >= SETTLE_AFTER:
                source = 'no-email-' + hashlib.sha256(request['id'].encode()).hexdigest()[:32]
                store.reply(request['id'], 'not submitted', source_kind=NO_EMAIL, source_id=source, sender_id='no-confirmation-email')
                events.append({'event': 'submit_unconfirmed_no_email', 'job_id': job_id})
    return events
