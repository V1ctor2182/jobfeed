import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.config import PipelineConfig
from pipeline.discord_intake import MAX_BACKOFF, DiscordIntake, extract_urls, run
from pipeline.jobfeed_intake_cli import use_jobfeed

ROOT = Path(__file__).resolve().parents[1]
CHANNEL, USER, STRANGER = '50', '400', '999'
OK, PROBLEM = '%E2%9C%85', '%E2%9A%A0%EF%B8%8F'
use_jobfeed(PipelineConfig.discover(ROOT).jobfeed_root)


class FakeDiscord:
    """One channel. Enforced nonces return the earlier message, as Discord does."""

    def __init__(self):
        self.messages, self.reactions, self.nonces, self.next_id = [], [], {}, 1000
        self.fail_next = []  # exceptions raised by the next calls, one per call

    def say(self, author, content, bot=False, kind=0):
        self.next_id += 1
        message = {'id': str(self.next_id), 'type': kind, 'author': {'id': author, 'bot': bot}, 'content': content}
        self.messages.append(message)
        return message

    def replies(self, message_id=None):
        return [m for m in self.messages if m.get('reply_to') and m['reply_to'] in ([message_id] if message_id else [m['reply_to']])]

    def __call__(self, method, path, body=None):
        if self.fail_next:
            raise self.fail_next.pop(0)
        route, _, query = path.partition('?')
        parts = route.strip('/').split('/')
        assert parts[:2] == ['channels', CHANNEL]
        if method == 'GET':
            params = dict(pair.split('=') for pair in query.split('&'))
            after, limit = int(params.get('after', 0)), int(params['limit'])
            found = [m for m in self.messages if int(m['id']) > after]
            found = found[:limit] if 'after' in params else found[-limit:]
            return list(reversed(found))
        if method == 'POST':
            if body.get('enforce_nonce') and body['nonce'] in self.nonces:
                return self.nonces[body['nonce']]
            message = self.say('bot', body['content'], bot=True)
            message['reply_to'] = body['message_reference']['message_id']
            self.nonces[body['nonce']] = message
            return message
        if method == 'PUT':
            self.reactions.append((parts[3], parts[5]))
            return None
        raise AssertionError(path)


class FakeIntake:
    def __init__(self, outcomes=None):
        self.outcomes, self.calls = outcomes or {}, []

    async def __call__(self, urls):
        self.calls.append(list(urls))
        results = [{'index': i, 'url': url, 'result': self.outcomes.get(url, {'status': 'queued', 'source_job_id': 700 + i})}
                   for i, url in enumerate(urls)]
        return {'status': 'completed', 'results': results}


@pytest.fixture
def setup(tmp_path):
    discord, intake = FakeDiscord(), FakeIntake()
    discord.say(USER, 'old https://jobs.example.com/old')

    def make():
        return DiscordIntake(tmp_path / 'intake.sqlite3', http=discord, channel_id=CHANNEL, allowed_user_ids=[USER],
                             add_job_urls=intake, log=lambda event: None)
    service = make()
    service.poll()
    return discord, intake, service, make


def test_first_start_skips_history(setup):
    discord, intake, service, _ = setup
    service.poll()
    assert intake.calls == [] and discord.replies() == []


def test_only_allowed_people_with_urls_are_queued(setup):
    discord, intake, service, _ = setup
    discord.say(STRANGER, 'https://jobs.example.com/a')
    discord.say(USER, 'https://jobs.example.com/b', bot=True)
    discord.say(USER, 'lunch at noon?')
    discord.say(USER, 'https://jobs.example.com/c', kind=7)
    mine = discord.say(USER, 'apply to https://jobs.example.com/d please')
    service.poll()
    assert intake.calls == [['https://jobs.example.com/d']]
    assert [m['reply_to'] for m in discord.replies()] == [mine['id']]
    assert discord.reactions == [(mine['id'], OK)]


def test_urls_are_deduped_in_order_and_split_into_groups_of_twenty(setup):
    discord, intake, service, _ = setup
    urls = [f'https://jobs.example.com/{n}' for n in range(45)]
    discord.say(USER, ' '.join(f'<{url}>' for url in urls + urls[:3]))
    service.poll()
    assert intake.calls == [urls[:20], urls[20:40], urls[40:]]
    text = '\n'.join(m['content'] for m in discord.replies())
    assert all(len(m['content']) <= 2000 for m in discord.replies())
    assert text.count('Queued · job') == 45 and '45 newly queued' in text
    assert text.index(f'<{urls[44]}>') > text.index(f'<{urls[0]}>')


def test_extract_urls_drops_wrapping_punctuation():
    assert extract_urls('See [this](https://a.example/jobs/1), and <https://b.example/x?id=2>. Also https://a.example/jobs/1.') == [
        'https://a.example/jobs/1', 'https://b.example/x?id=2']


def test_mixed_outcomes_are_reported_per_url(setup):
    discord, intake, service, _ = setup
    intake.outcomes = {
        'https://jobs.example.com/new': {'status': 'queued', 'source_job_id': 11, 'queue_state': 'Queued'},
        'https://jobs.example.com/known': {'status': 'already_queued', 'source_job_id': 12, 'queue_state': 'Cancelled'},
        'https://jobs.example.com/done': {'status': 'already_applied', 'source_job_id': 13},
        'https://jobs.example.com/gone': {'status': 'closed', 'reason': 'The posting is closed'},
    }
    message = discord.say(USER, ' '.join(intake.outcomes) + ' http://localhost/job')
    service.poll()
    assert intake.calls == [list(intake.outcomes)]
    [reply] = discord.replies(message['id'])
    lines = reply['content'].splitlines()
    assert lines[0] == 'Jobfeed intake with User priority: 1 newly queued, 1 already queued, 1 already applied, 2 not queued.'
    assert lines[1] == '1. Queued · job 11 · <https://jobs.example.com/new>'
    assert lines[2] == '2. Already queued · job 12 (Cancelled) · <https://jobs.example.com/known>'
    assert lines[3] == '3. Already applied · job 13 · <https://jobs.example.com/done>'
    assert lines[4] == '4. Closed · <https://jobs.example.com/gone>: The posting is closed'
    assert lines[5].startswith('5. Not a job URL · <http://localhost/job>: This must be an exact public')
    assert 'Nothing starts applying' in lines[6] and 'http://127.0.0.1:8765/apply-health' in lines[6] and 'User priority' in lines[6]
    assert discord.reactions == [(message['id'], PROBLEM)]


def test_restart_after_reply_posts_nothing_twice(setup):
    discord, intake, service, make = setup
    message = discord.say(USER, 'https://jobs.example.com/a')
    real_put = discord.__call__

    class Crash(Exception):
        pass

    def crash_on_put(method, path, body=None):
        if method == 'PUT':
            raise Crash
        return real_put(method, path, body)
    service.http = crash_on_put
    with pytest.raises(Crash):
        service.poll()
    assert len(discord.replies(message['id'])) == 1
    make().poll()
    assert intake.calls == [['https://jobs.example.com/a']]
    assert len(discord.replies(message['id'])) == 1
    assert discord.reactions == [(message['id'], OK)]


def test_restart_between_post_and_record_relies_on_the_nonce(setup):
    discord, intake, service, make = setup
    message = discord.say(USER, 'https://jobs.example.com/a')
    real = discord.__call__

    def crash_after_post(method, path, body=None):
        result = real(method, path, body)
        if method == 'POST':
            raise OSError('connection dropped after Discord created the message')
        return result
    service.http = crash_after_post
    with pytest.raises(OSError):
        service.poll()
    make().poll()
    assert len(discord.replies(message['id'])) == 1
    assert intake.calls == [['https://jobs.example.com/a']]


def test_server_errors_back_off_and_recover(setup):
    discord, intake, service, _ = setup
    message = discord.say(USER, 'https://jobs.example.com/a')
    discord.fail_next = [RuntimeError('Discord GET /channels/50/messages failed with 503'), OSError('network down'),
                         RuntimeError('Discord GET /channels/50/messages failed with 502')]
    sleeps, events = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(service, interval=3, log=events.append, sleep=sleep)
    assert sleeps == [6, 12, 24, 3, 3]
    assert [e['event'] for e in events] == ['error'] * 3 + ['polling']
    assert len(discord.replies(message['id'])) == 1


def test_backoff_is_capped(setup):
    discord, _, service, _ = setup
    discord.fail_next = [OSError('down')] * 20
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 10:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(service, interval=3, log=lambda event: None, sleep=sleep)
    assert max(sleeps) == MAX_BACKOFF


def test_cli_help_runs_without_discord():
    result = subprocess.run([sys.executable, '-m', 'pipeline.discord_intake', '--help'], capture_output=True, text=True,
                            cwd=ROOT, env={'PYTHONPATH': str(ROOT)}, timeout=30)
    assert result.returncode == 0 and '--root' in result.stdout
