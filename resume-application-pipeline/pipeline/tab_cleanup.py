"""Close the applying browser's tabs of jobs that are done, when no Applier is left to close them.

An Applier closes its own tabs when a tool result tells it to. That misses a job whose submission was confirmed after its
Applier ended, for example by the employer's email. Its tabs stay open for good. One pass here closes such tabs over
DevTools and touches nothing else.

A tab is closed only when it can be tied to jobs and every one of them is done. A tab belongs to a job when its current
address, or an address in its history, is one that job's Applier registered. A job is done when it was applied to or
skipped and its Applier has ended. A job is also done when its Applier ended without any Submit record and the job went
back to Queued, was cancelled, or its posting closed: nothing was sent, and a later Applier starts again in a new tab. So
is an Applier run that a requeue retired, whose registered tabs the requeue archived, while no Applier runs the job again.
A tab that matches no job, or any job still in progress, stays open: an unsaved form is worth more than a tidy tab bar.

Two source jobs can be the same posting, one applied to and one still in progress, and register the same page with and
without a tracking query. So a tab also stays open when a job that is not done registered the same page, where two
addresses are the same page if their paths match and their queries do not name different things.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import sqlite3
import sys

ENDED = ('stopped', 'finished', 'failed')
# Queue states of a job no Applier works on, after which a new run, if any, starts from the beginning.
RETURNED = ('Queued', 'Cancelled', 'Unavailable')


def _address(url):
    return (url or '').split('#', 1)[0]


def same_page(a, b):
    """Whether two addresses are one page: same site and path, and no query key they both carry with different values."""
    one, two = urlsplit(a), urlsplit(b)
    if (one.scheme, one.netloc.lower(), one.path.rstrip('/')) != (two.scheme, two.netloc.lower(), two.path.rstrip('/')):
        return False
    first, second = dict(parse_qsl(one.query)), dict(parse_qsl(two.query))
    return all(first[key] == second[key] for key in first.keys() & second.keys())


def _jobfeed_ids(jobfeed_db, query, params=()):
    try:
        with closing(sqlite3.connect(Path(jobfeed_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            return {int(row[0]) for row in db.execute(query, params)}
    except sqlite3.Error:
        return set()


def done_jobs(store, jobfeed_db):
    """Jobs whose tabs nothing needs any more (see the module docstring), and the jobs each registered address belongs to."""
    launched = {int(r['job_id']): r['state'] for r in store._read('SELECT job_id,state FROM applier_launches', ())}
    ended = {job_id for job_id, state in launched.items() if state in ENDED}
    attempted = {int(r['job_id']) for r in store._read('SELECT job_id FROM applier_submits', ())}
    finished = {int(r['job_id']) for r in store._read("SELECT job_id FROM applier_submits WHERE state='submitted'", ())}
    finished |= {int(r['job_id']) for r in store._read('SELECT job_id FROM applier_skips', ())}
    finished |= _jobfeed_ids(jobfeed_db, 'SELECT source_job_id FROM real_job_applications')
    returned = _jobfeed_ids(jobfeed_db, f"SELECT job_id FROM application_queue WHERE state IN ({','.join('?' for _ in RETURNED)})", RETURNED)
    done = (finished | (returned - attempted)) & ended
    owners = {}
    for row in store._read('SELECT job_id,url FROM applier_tabs', ()):
        owners.setdefault(_address(row['url']), set()).add(int(row['job_id']))
    # A requeue retires a run only when it sent nothing, and archives its tabs. Once the job has a new launch, that run's
    # tabs wait for it to end, since the new Applier may be on the same page before it registers its tab.
    for row in store._read('SELECT job_id,retired FROM applier_requeues', ()):
        job_id = int(row['job_id'])
        if job_id in launched and job_id not in done:
            continue
        for tab in json.loads(row['retired'] or '{}').get('applier_tabs', []):
            owners.setdefault(_address(tab['url']), set()).add(job_id)
            done.add(job_id)
    return done, owners


def plan(pages, done, owners):
    """The pages to close: [{id, url, job_ids}]. pages is [{id, url, history}], history being the tab's earlier addresses."""
    closing_pages = []
    unfinished = [address for address, jobs in owners.items() if not jobs <= done]
    for page in pages:
        jobs, addresses = set(), {_address(page['url']), *(_address(u) for u in page.get('history') or ())}
        # A page often comes back with a trailing slash or a tracking query its Applier did not register.
        for address in addresses:
            for registered, owner_jobs in owners.items():
                if registered == address or same_page(address, registered):
                    jobs |= owner_jobs
        if any(same_page(address, other) for address in addresses for other in unfinished):
            continue
        if jobs and jobs <= done:
            closing_pages.append({'id': page['id'], 'url': page['url'], 'job_ids': sorted(jobs)})
    return closing_pages


def pages(cdp):
    """Every tab of the applying browser with its address and its history, read without bringing any tab to the front."""
    found = []
    for target in cdp.call('Target.getTargets')['targetInfos']:
        if target.get('type') != 'page':
            continue
        history = []
        try:
            session = cdp.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
            history = [entry['url'] for entry in cdp.call('Page.getNavigationHistory', session=session).get('entries', [])]
            cdp.call('Target.detachFromTarget', {'sessionId': session})
        except Exception:  # noqa: BLE001 - a tab that cannot be read is judged by its address alone
            pass
        found.append({'id': target['targetId'], 'url': target.get('url', ''), 'history': history})
    return found


def sweep(store, jobfeed_db, cdp, *, dry_run=False):
    """One pass. Returns the tabs it closed, or would close when dry_run is set."""
    done, owners = done_jobs(store, jobfeed_db)
    chosen = plan(pages(cdp), done, owners)
    if not dry_run:
        for page in chosen:
            cdp.call('Target.closeTarget', {'targetId': page['id']})
    return chosen


def main(argv=None):
    from pipeline.applier_runtime import ApplierRuntime
    from pipeline.browser_broker import Chromium
    from pipeline.config import PipelineConfig
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=None)
    parser.add_argument('--dry-run', action='store_true', help='List the tabs a pass would close, and close none')
    args = parser.parse_args(argv)
    config = PipelineConfig.discover(args.root)
    chromium = Chromium()
    try:
        chosen = sweep(ApplierRuntime(config.resume_root), config.jobfeed_db, chromium.session(), dry_run=args.dry_run)
    finally:
        chromium.drop()
    print(json.dumps({'dry_run': args.dry_run, 'tabs': chosen}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
