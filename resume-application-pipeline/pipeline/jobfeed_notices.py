"""Jobfeed activity as short notices in the Discord channel itself. Channel messages notify the user; thread messages do not.

Each pass reads finished scans, answer bank changes, queue additions, Apply handoffs and status moves read-only, and sends
what is new since that source's cursor. The first pass only sets the cursors, so history is never sent. A source that
cannot be read is skipped for that pass.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from textwrap import shorten

from pipeline.answer_bank import APPLIER_AUTO, AnswerBank
from pipeline.local_state import read_only

SOURCES = ('runs', 'answers', 'queue', 'handoffs', 'statuses')
LISTED = 5
STATUS_TEXT = {'shortlisted': 'Moved {} to Wait', 'ignored': 'Ignored {}', 'applied': 'Marked {} as applied', 'scored': 'Moved {} back to Results'}
# Scoring writes these moves, and the scan notice already counts them.
SCORING_REASONS = ('auto_scored', 'legacy_evaluation_backfill')
# Answers saved approved at once, with no draft waiting for review first, by approval source.
DIRECT_SAVES = {'user_reply': 'Answer saved from your reply: ', APPLIER_AUTO: 'Answer the Applier decided under auto submit: '}


def _jobs(count):
    return f"{count} job{'' if count == 1 else 's'}"


def _listed(names):
    return ', '.join(names[:LISTED]) + (f', +{len(names) - LISTED} more' if len(names) > LISTED else '')


def _digest(values):
    return hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()[:16]


def run_text(run):
    """One finished scan or scoring run, with its counts, cost and failed sources."""
    kind = 'Scoring' if run['source'] == 'evaluate' else 'Scan' if run['source'] == 'all' else f"Scan of {run['source']}"
    outcome = {'user_stopped': 'stopped', 'interrupted': 'was interrupted'}.get(run['failure_code'], 'failed' if run['status'] == 'failed' else 'finished')
    if run['source'] == 'evaluate':
        verdicts = json.loads(run['verdict_counts_json'] or '{}')
        counts = f"{run['jobs_scored']} scored" + (' (' + ', '.join(f'{k.capitalize()} {v}' for k, v in verdicts.items()) + ')' if verdicts else '')
    else:
        counts = f"{run['jobs_discovered']} found, {run['jobs_inserted']} new, {run['jobs_updated']} updated"
    text = f'{kind} {outcome}: {counts}.'
    if run['total_llm_cost_usd']:
        text += f" Cost ${run['total_llm_cost_usd']:.2f}."
    failed = [f"{name} ({p.get('message') or 'failed'})" for name, p in json.loads(run['scan_progress_json'] or '{}').items() if p.get('phase') == 'failed']
    if failed:
        return text + ' Failed sources: ' + ', '.join(failed) + '.'
    return text + (f" {run['failure_message']}" if outcome == 'failed' and run['failure_message'] else '')


def answer_events(store):
    """(time, text) for each answer saved, saved for review, approved or revoked. Imported legacy drafts are not news."""
    for entry in store['entries']:
        for version in entry['versions']:
            question = shorten(version['question'], 100, placeholder='…')
            pair = f"{question} → {shorten(version['answer'], 150, placeholder='…')}"
            approval = version.get('approval') or {}
            source = approval.get('source', {})
            direct = DIRECT_SAVES.get(source.get('kind'))
            saved = source.get('action') == 'save'
            if 'imported_status' not in version and not direct and not saved:
                yield version['created_at'], f'Answer saved for review: {pair}'
            if approval:
                prefix = direct or ('Answer saved: ' if saved else 'Answer approved: ')
                yield approval['approved_at'], prefix + pair
            if version.get('revoked_at'):
                yield version['revoked_at'], f'Answer revoked: {question}'


class JobfeedNotices:
    def __init__(self, root, jobfeed_db, runtime_db):
        # runtime_db() opens the bridge's own database, which holds the cursors and the job threads.
        self.root, self.jobfeed_db, self.runtime_db = root, jobfeed_db, runtime_db
        self.failing = set()

    def post(self, send):
        """send(ref, text) posts one notice once per ref. Each cursor moves only after its notices are sent."""
        events = []
        with closing(self.runtime_db()) as db:
            cursors = {row['source']: json.loads(row['value']) for row in db.execute('SELECT source,value FROM applier_discord_cursors')}
        for source in SOURCES:
            try:
                cursor, notes = getattr(self, '_' + source)(cursors.get(source))
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
                if source not in self.failing:
                    events.append({'event': 'notice_source_failed', 'source': source, 'error': str(exc)})
                self.failing.add(source)
                continue
            self.failing.discard(source)
            for ref, text in notes:
                send(f'{source}:{ref}', text)
                events.append({'event': 'notice_posted', 'source': source})
            if source not in cursors or cursor != cursors[source]:
                with closing(self.runtime_db()) as db, db:
                    db.execute('INSERT OR REPLACE INTO applier_discord_cursors VALUES(?,?)', (source, json.dumps(cursor)))
        return events

    def _names(self, job_ids):
        """Company and title of each Jobfeed job, in order. The names only decorate a notice, so a failed lookup shows ids."""
        ids = list(dict.fromkeys(job_ids))
        try:
            with closing(read_only(self.jobfeed_db)) as db:
                rows = db.execute(f"SELECT id,company,title FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        except sqlite3.Error:
            rows = []
        found = {row['id']: ' · '.join(part for part in (row['company'], row['title']) if part) for row in rows}
        return [found.get(job_id) or f'job {job_id}' for job_id in ids]

    def _runs(self, cursor):
        with closing(read_only(self.jobfeed_db)) as db:
            runs = db.execute("SELECT * FROM pipeline_runs WHERE status != 'running' AND finished_at > ? ORDER BY finished_at", (cursor or '',)).fetchall()
        latest = runs[-1]['finished_at'] if runs else cursor or ''
        return latest, [] if cursor is None else [(run['run_id'], run_text(run)) for run in runs]

    def _answers(self, cursor):
        events = sorted(answer_events(AnswerBank(self.root).read()))
        latest = events[-1][0] if events else cursor or ''
        if cursor is None or not (new := [text for at, text in events if at > cursor]):
            return latest, []
        more = f'\n+{len(new) - LISTED} more changes in the Answer bank.' if len(new) > LISTED else ''
        return latest, [(latest, '\n'.join(new[:LISTED]) + more)]

    def _queue(self, cursor):
        with closing(read_only(self.jobfeed_db)) as db:
            rows = db.execute('SELECT job_id,manual_priority FROM application_queue ORDER BY job_id').fetchall()
        ids = [row['job_id'] for row in rows]
        if cursor is None or not (new := [row for row in rows if row['job_id'] not in set(cursor)]):
            return ids, []
        priority = ' with User priority' if all(row['manual_priority'] for row in new) else ''
        added = [row['job_id'] for row in new]
        return ids, [(_digest(added), f'Added {_jobs(len(new))} to the application queue{priority}: {_listed(self._names(added))}.')]

    def _handoffs(self, cursor):
        """Each Apply handoff from the dashboard, once the queue has admitted or refused its jobs."""
        with closing(read_only(self.root / '.pipeline/dashboard-queue.sqlite3')) as db:
            rows = db.execute("SELECT request_id,status,items,created_at FROM preparation_requests WHERE intent='handoff' AND created_at > ? ORDER BY created_at",
                              (cursor or '',)).fetchall()
        if cursor is None:
            return rows[-1]['created_at'] if rows else '', []
        notes = []
        for row in rows:
            if row['status'] == 'admitting':
                break
            cursor, items = row['created_at'], json.loads(row['items'])
            sent = [item['source_job_id'] for item in items if item['status'] != 'rejected']
            lines = [f'Sent {_jobs(len(sent))} to Apply: {_listed(self._names(sent))}.'] if sent else []
            refused = {}
            for item in items:
                if item['status'] == 'rejected':
                    refused[item['reason']] = refused.get(item['reason'], 0) + 1
            lines += [f'{_jobs(count)} not sent: {reason}' for reason, count in refused.items()]
            notes.append((row['request_id'], '\n'.join(lines)))
        return cursor, notes

    def _statuses(self, cursor):
        """Status moves made in Jobfeed, one notice per pass grouped by new status, so a bulk move is one message."""
        with closing(self.runtime_db()) as db:
            threads = [row['job_id'] for row in db.execute('SELECT job_id FROM applier_discord_threads WHERE job_id != 0')]
        with closing(read_only(self.jobfeed_db)) as db:
            latest = db.execute('SELECT COALESCE(MAX(id),0) FROM real_job_status_history').fetchone()[0]
            if cursor is None:
                return latest, []
            rows = db.execute('SELECT h.id,h.to_status,h.reason,h.real_job_id,r.representative_job_id FROM real_job_status_history h '
                              'LEFT JOIN real_jobs r ON r.id=h.real_job_id WHERE h.id>? AND h.id<=? AND h.from_status IS NOT NULL '
                              "AND COALESCE(h.reason,'') NOT IN (?,?) ORDER BY h.id", (cursor, latest, *SCORING_REASONS)).fetchall()
            # The job's own thread already says the Applier recorded it as applied.
            threaded = {row[0] for row in db.execute(f"SELECT real_job_id FROM jobs WHERE id IN ({','.join('?' * len(threads))})", threads)}
        rows = [row for row in rows if not (row['reason'] == 'record_application' and row['real_job_id'] in threaded)]
        if not rows:
            return latest, []
        moved = {}
        for row in rows:
            moved.setdefault(row['to_status'], {})[row['real_job_id']] = row['representative_job_id'] or row['real_job_id']
        lines = [STATUS_TEXT.get(status, 'Moved {} to ' + status.replace('_', ' ').capitalize()).format(_jobs(len(jobs))) + f': {_listed(self._names(jobs.values()))}.'
                 for status, jobs in moved.items()]
        return latest, [(f"{rows[0]['id']}-{rows[-1]['id']}", '\n'.join(lines))]
