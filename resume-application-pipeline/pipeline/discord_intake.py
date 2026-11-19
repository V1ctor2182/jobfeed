"""Queue job URLs posted in the Discord channel, with User priority, and reply with each result.

Always on and separate from the Applier worker. It only queues: nothing here starts an application, a worker or a
browser. It reads the channel by REST polling and never opens a gateway session, because a second session with the
bot token would also receive the user's button presses meant for the worker's bridge.

Each handled message keeps its intake results and its reply message IDs in a small store of its own, and the cursor
moves past a message only after its reply is posted. A restart resumes from what the store holds: it reuses the saved
results instead of importing again, and posts only the reply parts still missing. Each part carries a nonce that
Discord enforces, so a part sent just before a crash is not posted twice.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

from pipeline.applier_discord import DEFAULT_ENV, MAX_MESSAGE, http_client, load_env, main_channel
from pipeline.jobfeed_intake import MAX_BATCH_URLS, JobfeedIntake
from pipeline.jobfeed_intake_cli import OK_STATUSES, configured_db, url_problem, use_jobfeed

DASHBOARD = 'http://127.0.0.1:8765/apply-health'
OK_REACTION, PROBLEM_REACTION = '%E2%9C%85', '%E2%9A%A0%EF%B8%8F'
# Ordinary messages and replies. Joins, pins and other system messages are skipped.
USER_MESSAGE_TYPES = frozenset({0, 19})
PAGE = 100
MAX_BACKOFF = 300
URL_PATTERN = re.compile(r'https?://[^\s<>]+', re.IGNORECASE)
TRAILING = '.,;:!?)]}\'"*_~|'
LABELS = {'queued': 'Queued', 'already_queued': 'Already queued', 'already_applied': 'Already applied',
          'invalid_url': 'Not a job URL'}
FOOTER = f'This only queues. Nothing starts applying. Queue: <{DASHBOARD}> with the **User priority** filter.'
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS intake_cursor(channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_messages(message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, results TEXT,
                                           replies TEXT NOT NULL DEFAULT '[]', received_at TEXT NOT NULL);
'''


def extract_urls(text):
    """http(s) URLs in the order written, each once. Discord's <url> and markdown link brackets are not part of a URL."""
    urls = []
    for match in URL_PATTERN.findall(text or ''):
        url = match.rstrip(TRAILING)
        if url not in urls:
            urls.append(url)
    return urls


def _line(number, item):
    result = item['result']
    status = result.get('status', 'error')
    label = LABELS.get(status) or status.replace('_', ' ').capitalize()
    parts = [f"{number}. {label}"]
    if type(result.get('source_job_id')) is int:
        state = result.get('queue_state')
        parts.append(f"job {result['source_job_id']}" + (f' ({state})' if state and status != 'queued' else ''))
    parts.append(f"<{item['url']}>")
    line = ' · '.join(parts)
    if status not in OK_STATUSES and result.get('reason'):
        line += f": {result['reason']}"
    return line


def reply_parts(results):
    """The reply as messages under Discord's size limit: a count, one line per URL, then the reminder."""
    counts = {}
    for item in results:
        status = item['result'].get('status')
        key = status if status in OK_STATUSES else 'failed'
        counts[key] = counts.get(key, 0) + 1
    summary = ', '.join(f"{counts[key]} {name}" for key, name in (
        ('queued', 'newly queued'), ('already_queued', 'already queued'), ('already_applied', 'already applied'), ('failed', 'not queued'),
    ) if counts.get(key))
    lines = [f'Jobfeed intake with User priority: {summary}.', *(_line(n, item) for n, item in enumerate(results, 1)), FOOTER]
    parts, current = [], ''
    for line in lines:
        line = line if len(line) <= MAX_MESSAGE else line[:MAX_MESSAGE - 1] + '…'
        if current and len(current) + 1 + len(line) > MAX_MESSAGE:
            parts.append(current)
            current = line
        else:
            current = f'{current}\n{line}' if current else line
    return [*parts, current]


def all_ok(results):
    return all(item['result'].get('status') in OK_STATUSES for item in results)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _snowflake_now():
    return str((int(time.time() * 1000) - 1420070400000) << 22)


def _print(event):
    print(json.dumps({**event, 'at': _now()}), flush=True)


class DiscordIntake:
    """Poll one channel and queue the URLs the allowed senders post there.

    add_job_urls is JobfeedIntake.add_job_urls: an async call taking 1 to MAX_BATCH_URLS URLs.
    """

    def __init__(self, store_path, *, http, channel_id, allowed_user_ids, add_job_urls, log=_print):
        self.path, self.http, self.channel_id = Path(store_path), http, channel_id
        self.allowed, self.add_job_urls, self.log = frozenset(allowed_user_ids), add_job_urls, log

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.executescript(_SCHEMA)
        return db

    def _cursor(self):
        with closing(self._db()) as db:
            row = db.execute('SELECT message_id FROM intake_cursor WHERE channel_id=?', (self.channel_id,)).fetchone()
        return None if row is None else row['message_id']

    def _advance(self, message_id):
        with closing(self._db()) as db, db:
            db.execute('INSERT INTO intake_cursor VALUES(?,?) ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id',
                       (self.channel_id, message_id))

    def poll(self):
        """Handle every message after the cursor, oldest first. The first run only marks where to start."""
        cursor = self._cursor()
        if cursor is None:
            newest = self.http('GET', f'/channels/{self.channel_id}/messages?limit=1')
            cursor = newest[0]['id'] if newest else _snowflake_now()
            self._advance(cursor)
            self.log({'event': 'cursor_started', 'channel_id': self.channel_id, 'message_id': cursor})
            return
        while True:
            page = sorted(self.http('GET', f'/channels/{self.channel_id}/messages?limit={PAGE}&after={cursor}'), key=lambda m: int(m['id']))
            for message in page:
                self._handle(message)
                self._advance(message['id'])
            if len(page) < PAGE:
                return
            cursor = page[-1]['id']

    def _handle(self, message):
        author = message.get('author') or {}
        if author.get('bot') or author.get('id') not in self.allowed or message.get('type', 0) not in USER_MESSAGE_TYPES:
            return
        if not (urls := extract_urls(message.get('content'))):
            return
        with closing(self._db()) as db, db:
            db.execute('INSERT OR IGNORE INTO intake_messages(message_id,channel_id,received_at) VALUES(?,?,?)',
                       (message['id'], self.channel_id, _now()))
            row = db.execute('SELECT results,replies FROM intake_messages WHERE message_id=?', (message['id'],)).fetchone()
        if row['results'] is None:
            results = self._intake(urls)
            with closing(self._db()) as db, db:
                db.execute('UPDATE intake_messages SET results=? WHERE message_id=?', (json.dumps(results), message['id']))
            self.log({'event': 'intake', 'message_id': message['id'],
                      'results': [{'url': item['url'], 'status': item['result'].get('status'), 'job_id': item['result'].get('source_job_id')}
                                  for item in results]})
        else:
            results = json.loads(row['results'])
        replies = json.loads(row['replies'])
        for number, text in enumerate(reply_parts(results)[len(replies):], len(replies)):
            sent = self.http('POST', f'/channels/{self.channel_id}/messages', {
                'content': text, 'allowed_mentions': {'parse': []}, 'nonce': f"q{number}-{message['id']}", 'enforce_nonce': True,
                'message_reference': {'message_id': message['id'], 'fail_if_not_exists': False},
            })
            replies.append(sent['id'])
            with closing(self._db()) as db, db:
                db.execute('UPDATE intake_messages SET replies=? WHERE message_id=?', (json.dumps(replies), message['id']))
        emoji = OK_REACTION if all_ok(results) else PROBLEM_REACTION
        try:
            self.http('PUT', f"/channels/{self.channel_id}/messages/{message['id']}/reactions/{emoji}/@me")
        except (RuntimeError, OSError) as exc:
            # The reply already carries the outcome. A missing reaction, say on a deleted message, must not hold the cursor.
            self.log({'event': 'reaction_failed', 'message_id': message['id'], 'error': str(exc)[:200]})
        self.log({'event': 'replied', 'message_id': message['id'], 'replies': replies})

    def _intake(self, urls):
        """One result per URL, in order. Invalid URLs are reported without reaching the intake."""
        results = [{'url': url, 'result': {'status': 'invalid_url', 'reason': f'This {problem}.'}} if (problem := url_problem(url)) else None
                   for url in urls]
        pending = [index for index, item in enumerate(results) if item is None]
        for start in range(0, len(pending), MAX_BATCH_URLS):
            group = pending[start:start + MAX_BATCH_URLS]
            batch = asyncio.run(self.add_job_urls([urls[index] for index in group]))
            by_index = {item['index']: item['result'] for item in batch['results']}
            for offset, index in enumerate(group):
                results[index] = {'url': urls[index], 'result': by_index.get(offset) or {
                    'status': batch.get('status', 'error'), 'reason': batch.get('reason', 'The intake returned no result for this URL.')}}
        return results


def run(intake, *, interval, once=False, log=_print, sleep=time.sleep):
    """Poll forever. Discord errors and network failures back off exponentially up to MAX_BACKOFF seconds.
    A polling event after the first good poll, and hourly after that, shows the loop is alive."""
    failures, polls, last_report = 0, 0, None
    while True:
        try:
            intake.poll()
            failures, polls = 0, polls + 1
            if last_report is None or time.monotonic() - last_report >= 3600:
                log({'event': 'polling', 'polls': polls})
                last_report = time.monotonic()
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            failures += 1
            delay = min(interval * 2 ** failures, MAX_BACKOFF)
            log({'event': 'error', 'error': str(exc)[:300], 'retry_in': delay})
        else:
            delay = interval
        if once:
            return
        sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, required=True, help='The resume-application-pipeline directory')
    parser.add_argument('--env', type=Path, default=DEFAULT_ENV)
    parser.add_argument('--interval', type=float, default=3)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    from pipeline.config import PipelineConfig

    config = PipelineConfig.discover(args.root)
    use_jobfeed(config.jobfeed_root)
    try:
        db_path = configured_db(config.jobfeed_root)
    except ValueError as exc:
        parser.error(str(exc))
    env = load_env(args.env)
    http = http_client(env['DISCORD_BOT_TOKEN'])
    channel, allowed = main_channel(env, http)
    channel = env.get('DISCORD_INTAKE_CHANNEL_ID') or channel
    intake = DiscordIntake(config.resume_root / '.pipeline' / 'discord-intake.sqlite3', http=http, channel_id=channel,
                           allowed_user_ids=allowed, add_job_urls=JobfeedIntake(db_path, config.jobfeed_root).add_job_urls)
    _print({'event': 'started', 'channel_id': channel, 'allowed_user_ids': sorted(allowed), 'jobfeed_db': str(db_path),
            'cursor': intake._cursor(), 'interval': args.interval})
    run(intake, interval=args.interval, once=args.once)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
