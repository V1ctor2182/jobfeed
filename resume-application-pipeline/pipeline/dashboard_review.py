"""The Dashboard's Review page: what Appliers are waiting on the user for, and the user's own replies.

Each reply is the user's click in the local Dashboard. Jobfeed's route checks the request came from that Dashboard before
it runs this program. Replies are saved with source `dashboard`, exactly as a Discord button press saves them, so the
waiting Applier picks them up the same way. Nothing here confirms Submit.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from pipeline.applier_discord import BLOCKER_WORDS
from pipeline import site_holds
from pipeline.confirmation_mail import ask_user_now
from pipeline.applier_runtime import ApplierRuntime

ROOT = Path(__file__).resolve().parents[1]
SENDER = 'dashboard-user'
IGNORE_REASON = 'Pressed Ignore in the Dashboard'


def _jobs(jobfeed_db, job_ids):
    if jobfeed_db is None or not job_ids:
        return {}
    try:
        with closing(sqlite3.connect(Path(jobfeed_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            marks = ','.join('?' for _ in job_ids)
            return {row[0]: row[1:] for row in db.execute(f'SELECT id,company,title,url FROM jobs WHERE id IN ({marks})', tuple(job_ids))}
    except sqlite3.Error:
        return {}


ENDED = ('stopped', 'finished', 'failed')


def open_requests(store):
    """Open requests split by whether their Applier can still take a reply. An ended Applier cannot."""
    ended = {int(row['job_id']) for row in store._read(f"SELECT job_id FROM applier_launches WHERE state IN ({','.join('?' for _ in ENDED)})", ENDED)}
    known = {int(row['job_id']) for row in store._read('SELECT job_id FROM applier_launches', ())}
    requests = [store.request(row['id']) for row in store.requests() if row['state'] == 'open']
    live = [r for r in requests if int(r['job_id']) in known - ended]
    return live, len(requests) - len(live)


def open_items(store, jobfeed_db=None):
    """Every open question and blocker a running or parked Applier waits on. Questions come first, then oldest first."""
    requests, _ = open_requests(store)
    # Whether Submit went through is read from the employer's email, never asked, unless that check has not run for hours.
    requests = [r for r in requests if r['blocker_type'] != 'submit_unknown' or ask_user_now(r)]
    jobs = _jobs(jobfeed_db, sorted({int(r['job_id']) for r in requests}))
    items = []
    for request in sorted(requests, key=lambda r: (r['kind'] != 'question', r['created_at'])):
        company, title, url = jobs.get(int(request['job_id']), (request.get('company'), None, None))
        blocker = request['kind'] == 'blocker'
        items.append({
            'id': request['id'], 'job_id': int(request['job_id']), 'kind': request['kind'], 'blocker_type': request['blocker_type'],
            'company': company or request.get('company'), 'title': title, 'posting_url': url,
            'page_url': (request.get('evidence') or {}).get('page_url'), 'field': request['field'], 'text': request['text'],
            'draft': request.get('draft'), 'options': request.get('options') or [],
            'quick_replies': list(BLOCKER_WORDS.get(request['blocker_type'], ('done',))) if blocker else [],
            'created_at': request['created_at'],
        })
    return items


def reply(store, payload):
    """Save the user's answer, pick, draft approval or change request for one open request."""
    request = store.request(payload['request_id'])
    if request['state'] != 'open':
        raise ValueError('This question is already closed.')
    action, text = payload['action'], (payload.get('text') or '').strip()
    if action == 'approve':
        if not request.get('draft'):
            raise ValueError('This question has no draft.')
        text = request['draft']
    elif action == 'pick':
        if text not in (request.get('options') or []):
            raise ValueError('That choice does not fit this question.')
    elif action == 'quick':
        if request['kind'] != 'blocker' or text not in BLOCKER_WORDS.get(request['blocker_type'], ('done',)):
            raise ValueError('That reply does not fit this blocker.')
    elif action not in {'answer', 'change_request'}:
        raise ValueError('unknown action')
    intent = 'change_request' if action == 'change_request' else 'answer'
    store.reply(request['id'], text, source_kind='dashboard', source_id=payload['click_id'], sender_id=SENDER, intent=intent)
    return {'request_id': request['id'], 'intent': intent}


def ignore(store, payload):
    """Record the user's Ignore for one job: its Applier stops and the job is cancelled in the queue."""
    job_id = int(payload['job_id'])
    if store.skip(job_id):
        raise ValueError(f'Job {job_id} is already skipped.')
    store.ignore(job_id, source_id=payload['click_id'], sender_id=SENDER, source_kind='dashboard', reason=IGNORE_REASON)
    return {'job_id': job_id}


def site_groups(store, jobfeed_db):
    """One action per job board that stopped several Appliers at a sign-in, and whether its queued jobs are held."""
    if jobfeed_db is None:
        return []
    return site_holds.groups(store, jobfeed_db)


def resume_site(store, jobfeed_db, payload):
    """The user's own resume of one job board after signing in there."""
    return site_holds.resume(store, jobfeed_db, payload['site'], click_id=payload['click_id'], sender=SENDER)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--jobfeed-db', type=Path)
    parser.add_argument('command', choices=['list', 'reply', 'ignore', 'resume-site'])
    args = parser.parse_args(argv)
    store = ApplierRuntime(args.root)
    try:
        payload = json.load(sys.stdin)
        if args.command == 'reply':
            reply(store, payload)
        elif args.command == 'ignore':
            ignore(store, payload)
        elif args.command == 'resume-site':
            resume_site(store, args.jobfeed_db, payload)
        print(json.dumps({'items': open_items(store, args.jobfeed_db), 'ended': open_requests(store)[1], 'sites': site_groups(store, args.jobfeed_db)}))
        return 0
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'code': 'invalid', 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
