"""Site holds: stop starting jobs from a job board while every Applier there stops at the same sign-in.

An Applier never signs in or creates an account. When a job board asks for that before its Apply button works, each
Applier reports a user_only_check blocker and parks. Starting more jobs from that board only parks more Appliers. Once
SIGN_IN_AFTER of them have stopped on the board's own pages within RECENT, the worker holds the board: none of its jobs that have not
started is prepared or admitted. Started jobs and other boards go on. The user signs in once in the applying browser and
resumes the board from the Dashboard, which clears the hold and tells each stopped Applier to try again.

Only recent stops raise a hold. A parked Applier keeps its blocker open for days, long after the user has signed in and
later jobs from the board went through, so old blockers say nothing about the board now.

A hold is one row in the Applier store's control table. Jobfeed's database refuses tables it does not know.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

SIGN_IN_AFTER = 3
RECENT = timedelta(hours=6)
WORKER = 'worker: sign-in hold'
ENDED = ('stopped', 'finished', 'failed')
RESUME_REPLY = 'done'
_KEY = 'site-hold:'


def _platforms(jobfeed_db, job_ids):
    if not job_ids:
        return {}
    try:
        with closing(sqlite3.connect(Path(jobfeed_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            marks = ','.join('?' for _ in job_ids)
            return {int(row[0]): row[1] for row in db.execute(f'SELECT id,platform FROM jobs WHERE id IN ({marks})', tuple(job_ids)) if row[1]}
    except sqlite3.Error:
        return {}


def active(store):
    """The held job boards: {site: {reason, raised_at, raised_by}}."""
    holds = {}
    for row in store._read('SELECT key,value FROM applier_control WHERE key LIKE ?', (_KEY + '%',)):
        try:
            holds[row['key'][len(_KEY):]] = json.loads(row['value'])
        except ValueError:
            continue
    return holds


def hold(store, site, reason, raised_by):
    """Hold one job board. Returns whether this call raised the hold; an active hold is left as it is."""
    if site in active(store):
        return False
    store.set_control(_KEY + site, json.dumps({'reason': reason, 'raised_at': datetime.now(timezone.utc).isoformat(), 'raised_by': raised_by}))
    return True


def clear(store, site):
    """Let one job board's jobs start again. Returns whether it was held."""
    held = site in active(store)
    if held:
        store.set_control(_KEY + site, None)
    return held


def held_jobs(store, jobfeed_db, job_ids):
    """The jobs among job_ids whose job board is held. One read of the holds, and no database read when nothing is held."""
    sites = set(active(store))
    if not sites:
        return set()
    return {job_id for job_id, site in _platforms(jobfeed_db, sorted(set(job_ids))).items() if site in sites}


def blocked_sites(store, jobfeed_db):
    """Open user_only_check blockers of Appliers that can still continue, grouped by the job board whose own page stopped
    them. A blocker on an employer's site says nothing about the board, so it is left out."""
    ended = {int(row['job_id']) for row in store._read(f"SELECT job_id FROM applier_launches WHERE state IN ({','.join('?' for _ in ENDED)})", ENDED)}
    requests = [store.request(row['id']) for row in store.requests()
                if row['state'] == 'open' and row['blocker_type'] == 'user_only_check' and int(row['job_id']) not in ended]
    platforms = _platforms(jobfeed_db, sorted({int(r['job_id']) for r in requests}))
    sites = {}
    for request in sorted(requests, key=lambda r: r['created_at']):
        site = platforms.get(int(request['job_id']))
        host = (urlsplit((request.get('evidence') or {}).get('page_url') or '').hostname or '').lower()
        if site and site.lower() in host.split('.'):
            sites.setdefault(site, []).append(request)
    return sites


def queued_on(jobfeed_db, site):
    try:
        with closing(sqlite3.connect(Path(jobfeed_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute("SELECT count(*) FROM application_queue q JOIN jobs j ON j.id=q.job_id WHERE q.state='Queued' AND j.platform=?", (site,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def recent(requests, now=None):
    """The blockers raised within RECENT."""
    since = (now or datetime.now(timezone.utc)) - RECENT
    return [r for r in requests if datetime.fromisoformat(r['created_at']) >= since]


def park(pipeline, store, now=None):
    """One worker pass: hold each job board that stopped SIGN_IN_AFTER Appliers at a sign-in within RECENT."""
    events = []
    for site, found in blocked_sites(store, pipeline.config.jobfeed_db).items():
        requests = recent(found, now)
        if len(requests) < SIGN_IN_AFTER:
            continue
        reason = f'{len(requests)} Appliers stopped at a sign-in on {site}. Its queued jobs wait until the user signs in and resumes it.'
        if hold(store, site, reason, WORKER):
            events.append({'event': 'site_held', 'site': site, 'blocked': len(requests)})
    return events


def groups(store, jobfeed_db):
    """What the Dashboard shows as one action per job board: how many Appliers it stopped and whether it is held."""
    held = active(store)
    blocked = blocked_sites(store, jobfeed_db)
    result = []
    for site in sorted(set(held) | {s for s, requests in blocked.items() if len(requests) >= SIGN_IN_AFTER}):
        requests = blocked.get(site, [])
        result.append({'site': site, 'label': f'Sign in to {site.replace("_", " ").title()}', 'held': site in held,
                       'request_ids': [r['id'] for r in requests], 'job_ids': sorted({int(r['job_id']) for r in requests}),
                       'queued': queued_on(jobfeed_db, site),
                       'latest_stop_at': max((r['created_at'] for r in requests), default=None),
                       'page_url': next(((r.get('evidence') or {}).get('page_url') for r in requests), None)})
    return result


def resume(store, jobfeed_db, site, *, click_id, sender):
    """The user's own resume of one job board: clear its hold and tell each Applier it stopped to try again."""
    requests = blocked_sites(store, jobfeed_db).get(site, [])
    cleared = clear(store, site)
    if not cleared and not requests:
        raise ValueError(f'{site} is not held and has no stopped Appliers.')
    for request in requests:
        store.reply(request['id'], RESUME_REPLY, source_kind='dashboard', source_id=f'{click_id}-{request["id"][:12]}', sender_id=sender)
    return {'site': site, 'cleared': cleared, 'woken': len(requests)}
