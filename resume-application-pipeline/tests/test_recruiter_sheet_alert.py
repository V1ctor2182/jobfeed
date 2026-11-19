import json
import urllib.error
import urllib.request

import pytest

from pipeline import recruiter_sheet_alert as alert

CHANNEL = '50'
FAILED = False
OK = True


class FakeDiscord:
    def __init__(self):
        self.posts, self.fail_next = [], 0

    def __call__(self, method, path, body=None):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError('Discord POST /channels/50/messages failed with 500')
        self.posts.append((method, path, body))
        return {'id': '1'}

    def contents(self):
        return [body['content'] for _, _, body in self.posts]


@pytest.fixture
def discord():
    return FakeDiscord()


def run(tmp_path, discord, succeeded, reason='the Jobs tab has an unexpected header, so nothing changed'):
    return alert.run(tmp_path, succeeded, reason, connect=lambda: (discord, CHANNEL))


def state(tmp_path):
    return json.loads((tmp_path / '.pipeline' / alert.STATE_NAME).read_text())


def test_one_failure_says_nothing_because_it_retries_itself(tmp_path, discord):
    assert run(tmp_path, discord, FAILED) == 0
    assert discord.posts == []
    assert state(tmp_path) == {'consecutive_failures': 1, 'alert_outstanding': False}


def test_the_second_failure_in_a_row_posts_once_to_the_main_channel(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED)
    [(method, path, body)] = discord.posts
    assert (method, path) == ('POST', f'/channels/{CHANNEL}/messages')
    assert 'failed twice in a row' in body['content']
    assert state(tmp_path) == {'consecutive_failures': 2, 'alert_outstanding': True}


def test_the_failure_message_says_what_is_wrong_and_where_to_look(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED)
    [content] = discord.contents()
    assert 'may be stale' in content
    assert 'may not be reaching the pipeline' in content
    assert "Autopilot's hold still protects" in content
    assert '```\nthe Jobs tab has an unexpected header, so nothing changed\n```' in content
    assert "journalctl --user -u jobfeed-dashboard.service -g 'recruiter.sheet' -n 50" in content


def test_mentions_are_disabled_on_every_message(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED, reason='@everyone look')
    run(tmp_path, discord, OK)
    assert len(discord.posts) == 2
    assert all(body['allowed_mentions'] == {'parse': []} for _, _, body in discord.posts)


def test_no_more_messages_while_it_stays_failed(tmp_path, discord):
    for _ in range(5):
        run(tmp_path, discord, FAILED)
    assert len(discord.posts) == 1
    assert state(tmp_path)['consecutive_failures'] == 5


def test_success_after_an_alert_posts_one_recovery_and_resets(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, OK)
    run(tmp_path, discord, OK)
    assert len(discord.posts) == 2
    assert 'working again' in discord.contents()[1]
    assert state(tmp_path) == {'consecutive_failures': 0, 'alert_outstanding': False}


def test_success_after_one_failure_posts_nothing_and_resets_the_count(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, OK)
    run(tmp_path, discord, FAILED)
    assert discord.posts == []
    assert state(tmp_path)['consecutive_failures'] == 1


def test_a_failed_post_tries_again_on_the_next_failed_run(tmp_path, discord, capsys):
    run(tmp_path, discord, FAILED)
    discord.fail_next = 1
    assert run(tmp_path, discord, FAILED) == 1
    assert 'alert_post_failed' in capsys.readouterr().out
    assert state(tmp_path) == {'consecutive_failures': 2, 'alert_outstanding': False}
    run(tmp_path, discord, FAILED)
    [content] = discord.contents()
    assert 'failed 3 times in a row' in content
    assert state(tmp_path) == {'consecutive_failures': 3, 'alert_outstanding': True}


def test_a_failed_recovery_post_tries_again_on_the_next_good_run(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED)
    discord.fail_next = 1
    assert run(tmp_path, discord, OK) == 1
    assert state(tmp_path) == {'consecutive_failures': 0, 'alert_outstanding': True}
    run(tmp_path, discord, OK)
    assert len(discord.posts) == 2
    assert state(tmp_path) == {'consecutive_failures': 0, 'alert_outstanding': False}


def test_a_failure_to_connect_is_a_failed_post(tmp_path, capsys):
    def connect():
        raise KeyError('DISCORD_BOT_TOKEN')
    for _ in range(2):
        code = alert.run(tmp_path, FAILED, 'boom', connect=connect)
    assert code == 1
    assert 'alert_post_failed' in capsys.readouterr().out
    assert state(tmp_path) == {'consecutive_failures': 2, 'alert_outstanding': False}


@pytest.mark.parametrize('text', ['not json', '[]', '{"consecutive_failures": "x", "alert_outstanding": true}',
                                  '{"consecutive_failures": -1, "alert_outstanding": false}', '{"alert_outstanding": 1}'])
def test_corrupt_state_counts_as_healthy(tmp_path, discord, text):
    path = tmp_path / '.pipeline' / alert.STATE_NAME
    path.parent.mkdir()
    path.write_text(text)
    run(tmp_path, discord, FAILED)
    assert discord.posts == []
    assert state(tmp_path) == {'consecutive_failures': 1, 'alert_outstanding': False}


def test_the_reason_is_truncated_to_300_characters(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED, reason='x' * 500)
    [content] = discord.contents()
    assert f"```\n{'x' * 300}\n```" in content


def test_backticks_are_stripped_so_the_reason_cannot_leave_the_code_block(tmp_path, discord):
    run(tmp_path, discord, FAILED)
    run(tmp_path, discord, FAILED, reason='bad ``` @everyone `x`')
    [content] = discord.contents()
    assert '```\nbad @everyone x\n```' in content
    assert content.count('`') == 6 + 2


@pytest.mark.parametrize('reason', ['', '  \n ', '`````'])
def test_a_reason_with_nothing_to_show_says_so(tmp_path, discord, reason):
    run(tmp_path, discord, FAILED, reason=reason)
    run(tmp_path, discord, FAILED, reason=reason)
    [content] = discord.contents()
    assert f'```\n{alert.NO_REASON}\n```' in content


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def fake_urlopen(sent, *, fail_post=False):
    def urlopen(request, timeout):
        sent.append(request)
        if request.get_method() != 'POST':
            return FakeResponse({'id': '9', 'owner_id': '1'})
        if fail_post:
            raise urllib.error.HTTPError(request.full_url, 500, 'no', {}, None)
        return FakeResponse({'id': '2'})
    return urlopen


def run_main(tmp_path, monkeypatch, urlopen):
    env = tmp_path / 'discord.env'
    env.write_text('DISCORD_BOT_TOKEN=s3cret-token\nDISCORD_GUILD_ID=9\nDISCORD_CHANNEL_ID=50\n')
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    args = ['--root', str(tmp_path), '--env', str(env), '--result', 'failure', '--reason', 'the reason']
    alert.main(args)
    return alert.main(args)


def test_main_reads_the_env_file_and_posts_through_the_bot_client(tmp_path, monkeypatch, capsys):
    sent = []
    assert run_main(tmp_path, monkeypatch, fake_urlopen(sent)) == 0
    post = next(request for request in sent if request.get_method() == 'POST')
    assert post.full_url.endswith('/channels/50/messages')
    assert post.get_header('Authorization') == 'Bot s3cret-token'
    body = json.loads(post.data)
    assert body['allowed_mentions'] == {'parse': []}
    assert 'the reason' in body['content']
    assert 's3cret-token' not in body['content']
    assert 's3cret-token' not in ''.join(capsys.readouterr())


def test_a_failed_post_through_the_bot_client_never_prints_the_token(tmp_path, monkeypatch, capsys):
    assert run_main(tmp_path, monkeypatch, fake_urlopen([], fail_post=True)) == 1
    output = ''.join(capsys.readouterr())
    assert 'alert_post_failed' in output
    assert 's3cret-token' not in output


def test_a_missing_env_file_is_a_failed_post(tmp_path, capsys):
    args = ['--root', str(tmp_path), '--env', str(tmp_path / 'missing.env'), '--result', 'failure', '--reason', 'boom']
    alert.main(args)
    assert alert.main(args) == 1
    assert 'alert_post_failed' in capsys.readouterr().out


def test_main_records_a_success_without_connecting(tmp_path):
    args = ['--root', str(tmp_path), '--env', str(tmp_path / 'missing.env'), '--result', 'success']
    assert alert.main(args) == 0
    assert state(tmp_path) == {'consecutive_failures': 0, 'alert_outstanding': False}


@pytest.mark.parametrize('result', [[], ['--result', 'timeout']])
def test_main_needs_a_known_result_and_changes_nothing_without_one(tmp_path, capsys, result):
    with pytest.raises(SystemExit) as stopped:
        alert.main(['--root', str(tmp_path), *result])
    assert stopped.value.code == 2
    assert '--result' in capsys.readouterr().err
    assert not (tmp_path / '.pipeline').exists()
