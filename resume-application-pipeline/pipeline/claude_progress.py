"""Progress of a Claude Applier in the cursor shape the progress monitor reads.

Read only: the stored launch record and the Applier runtime store. It never calls the claude CLI; the worker's
supervisor keeps the launch state current.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from pipeline.applier_runtime import read_only
from pipeline.applier_supervisor import DONE_STEPS

# An open request ends the turn for the user. An escalated one is a retry on the stronger model, not a question.
# Only a check the user must do in person, such as a CAPTCHA, is verification.
WAITING_REQUESTS = ('open',)
REQUEST_RESULTS = {'user_only_check': 'needs_verification', 'captcha': 'needs_verification'}
# A finished step, as a result status: 'ready for review' is ready_for_review.
DONE_RESULTS = {step: step.replace(' ', '_') for step in DONE_STEPS}
# Launch states without a waiting request or a finished step. Any other state is unknown.
LAUNCH_TURNS = {'requested': 'active', 'launched': 'active', 'running': 'active',
                'finished': 'interrupted', 'failed': 'interrupted', 'stopped': 'interrupted', 'parked': 'interrupted', 'launch_failed': 'interrupted'}


def _records(root, job_id):
    path = Path(root).resolve() / '.pipeline' / 'applier-runtime.sqlite3'
    if not path.exists():
        return None, None, None
    try:
        with closing(read_only(path)) as db:
            db.row_factory = sqlite3.Row
            launch = db.execute('SELECT launch_id,state,created_at,updated_at FROM applier_launches WHERE job_id=?', (job_id,)).fetchone()
            job = db.execute('SELECT last_progress,last_progress_at FROM applier_jobs WHERE job_id=?', (job_id,)).fetchone()
            marks = ','.join('?' for _ in WAITING_REQUESTS)
            request = db.execute(f'SELECT blocker_type,text,created_at FROM applier_requests WHERE job_id=? AND state IN ({marks}) '
                                 'ORDER BY created_at DESC LIMIT 1', (job_id, *WAITING_REQUESTS)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return None, None, None
        raise
    return (dict(launch) if launch else None), (dict(job) if job else {}), (dict(request) if request else None)


def claude_cursor(root, job_id, owner):
    """Return (cursor, problems) for the job's Claude Applier; the queue owner must be its launch ID."""
    launch, job, request = _records(root, job_id)
    if launch is None:
        return {'turn_state': 'unknown'}, ['claude_launch_missing']
    if launch['launch_id'] != owner:
        return {'turn_state': 'unknown'}, ['owner_conflict']
    progress_at, step = job.get('last_progress_at'), job.get('last_progress')
    cursor = dict(first_started_at=launch['created_at'], last_tool_at=progress_at or launch['created_at'])
    if request:
        at = request['created_at']
        cursor.update(turn_state='ended', turn_at=at, result_at=at,
                      result_status=REQUEST_RESULTS.get(request['blocker_type'], 'needs_input'),
                      blocker_kind=request['blocker_type'], result_reason=request['text'][:500])
    elif step in DONE_RESULTS:
        cursor.update(turn_state='ended', turn_at=progress_at, result_at=progress_at, result_status=DONE_RESULTS[step])
    else:
        state = LAUNCH_TURNS.get(launch['state'], 'unknown')
        cursor.update(turn_state=state, turn_at=cursor['last_tool_at'] if state == 'active' else launch['updated_at'])
    return cursor, []
