"""Connect the Appliers' Discord bot, or check the saved connection.

Appliers ask their questions in the user's own Discord server, through a bot the user makes. This checks the bot's
token and settings, posts one setup message with a thread in the server's main channel, and saves the token file
`~/.config/jobfeed-applier/discord.env`, readable by this user only. It prints one JSON object and never the token.

    python -m pipeline.discord_setup            # asks for the token without showing it, or reads it from stdin
    python -m pipeline.discord_setup --again    # after fixing a setting, retries with the token Discord accepted
    python -m pipeline.discord_setup --check    # checks the saved file and posts nothing

Exit code 0 means connected. Otherwise `problem` names what is missing and `fix` says how to fix it, in plain words.
Each run except --check also leaves its result in `~/.config/jobfeed-applier/discord-setup.json`, for when it ran in a
terminal window the agent cannot read.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from pipeline.applier_discord import DEFAULT_ENV, DiscordHTTPError, http_client, load_env, main_channel

# View Channel, Send Messages, Embed Links, Attach Files, Read Message History, Manage Threads, Create Public Threads,
# Send Messages in Threads: what the bridge needs to keep one thread per job.
PERMISSIONS = sum(1 << bit for bit in (10, 11, 14, 15, 16, 34, 35, 38))
# The application flags for the Message Content intent, approved or limited. Without it a reply's text reads as empty.
MESSAGE_CONTENT = (1 << 18) | (1 << 19)
PORTAL = 'https://discord.com/developers/applications'
WELCOME = ('✅ Jobfeed Appliers are connected. When an Applier needs you, it writes in a thread here, one thread per job. '
           'Reply in that thread. Only the server owner can answer.')


class Problem(Exception):
    def __init__(self, problem, fix, **extra):
        super().__init__(problem)
        self.problem, self.fix, self.extra = problem, fix, extra


def invite_url(application_id):
    return f'https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot&permissions={PERMISSIONS}'


def _get(http, path, problem, fix):
    try:
        return http('GET', path)
    except DiscordHTTPError as exc:
        if exc.code == 401:
            raise Problem('bad_token', f'Discord does not accept this token. In {PORTAL}, open your app, then Bot, '
                                       'click Reset Token and copy the new one.') from None
        raise Problem(problem, fix) from None
    except OSError:
        raise Problem('discord_unreachable', 'This server cannot reach discord.com. Check its internet connection.') from None


def check(http, server_id=None):
    """The bot's state: its name, its server and channel, and the senders it accepts. Raises Problem."""
    bot = _get(http, '/users/@me', 'bad_token', 'Copy the bot token again.')
    app = _get(http, '/applications/@me', 'bad_token', 'Copy the bot token again.')
    if not (app.get('flags') or 0) & MESSAGE_CONTENT:
        raise Problem('message_content_off', f'In {PORTAL}, open your app, then Bot, turn on Message Content Intent '
                                             'and click Save Changes.')
    guilds = _get(http, '/users/@me/guilds', 'discord_unreachable', 'Try again in a minute.')
    if server_id:
        guilds = [g for g in guilds if g['id'] == server_id]
    if not guilds:
        raise Problem('not_in_server', 'Add the bot to your Discord server: open invite_url, choose your server and '
                                       'click Authorize.', invite_url=invite_url(app['id']))
    if len(guilds) > 1:
        raise Problem('several_servers', 'The bot is in several servers. Run this again with --server and the ID of '
                                         'the one to use.', servers=[{'id': g['id'], 'name': g['name']} for g in guilds])
    guild = guilds[0]
    try:
        channel, _ = main_channel({'DISCORD_GUILD_ID': guild['id']}, http)
    except StopIteration:
        raise Problem('no_text_channel', 'Your Discord server has no text channel. Add one, then run this again.') from None
    except DiscordHTTPError:
        raise Problem('no_permission', 'The bot cannot see your server. Add it again with invite_url.', invite_url=invite_url(app['id'])) from None
    # The bridge accepts answers from the server owner only, unless DISCORD_ALLOWED_USER_IDS names others.
    return {'bot': bot.get('username'), 'server': guild['name'], 'server_id': guild['id'], 'channel_id': channel,
            'invite_url': invite_url(app['id'])}


def post_welcome(http, channel, invite):
    """Post the setup message and open a thread under it, which proves every permission the bridge uses."""
    try:
        message = http('POST', f'/channels/{channel}/messages', {'content': WELCOME})
        thread = http('POST', f"/channels/{channel}/messages/{message['id']}/threads", {'name': 'Jobfeed setup check'})
        http('POST', f"/channels/{thread['id']}/messages", {'content': 'Questions about one job appear in a thread like this one.'})
        http('PATCH', f"/channels/{thread['id']}", {'archived': True})
    except DiscordHTTPError:
        raise Problem('no_permission', 'The bot may not post or open threads in your server. Remove it from the server, '
                                       'then add it again with invite_url, keeping every permission it asks for.', invite_url=invite) from None


def _private_write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)
    path.chmod(0o600)


def _pending(env_path):
    """A token that Discord accepted while some other setting was still missing. The bridge never reads it."""
    return env_path.with_name(env_path.name + '.pending')


def read_token(stdin=sys.stdin):
    token = getpass.getpass('Discord bot token (hidden): ') if stdin.isatty() else stdin.readline()
    return token.strip()


def run(args, *, client=http_client, token_reader=read_token):
    env_path, pending = Path(args.env).expanduser(), _pending(Path(args.env).expanduser())
    if args.check:
        if not env_path.is_file():
            raise Problem('not_set_up', 'Discord is not connected yet. Run python -m pipeline.discord_setup.')
        env = load_env(env_path)
        return {'connected': True, **check(client(env['DISCORD_BOT_TOKEN']), env.get('DISCORD_GUILD_ID'))}
    if args.again:
        if not pending.is_file():
            raise Problem('no_token', 'No token is waiting. Run python -m pipeline.discord_setup and paste the token.')
        token = pending.read_text().strip()
    else:
        token = token_reader()
    if not token:
        raise Problem('no_token', 'Paste the bot token.')
    http = client(token)
    _get(http, '/users/@me', 'bad_token', 'Copy the bot token again.')
    # Discord accepts the token: keep it, so fixing a setting needs no second paste (--again).
    _private_write(pending, token + '\n')
    state = check(http, args.server)
    post_welcome(http, state['channel_id'], state['invite_url'])
    _private_write(env_path, f"DISCORD_BOT_TOKEN={token}\nDISCORD_GUILD_ID={state['server_id']}\n")
    pending.unlink(missing_ok=True)
    return {'connected': True, **state, 'saved': str(env_path)}


def main(argv=None, **kwargs):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--check', action='store_true', help='check the saved connection and post nothing')
    parser.add_argument('--again', action='store_true', help='retry with the token from the last run, after fixing a setting')
    parser.add_argument('--server', help='the Discord server ID, when the bot is in several')
    parser.add_argument('--env', default=str(DEFAULT_ENV), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run(args, **kwargs)
    except Problem as exc:
        result = {'connected': False, 'problem': exc.problem, 'fix': exc.fix, **exc.extra}
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if not args.check:
        # The last result, without the token, for an agent that cannot see the terminal it ran in.
        _private_write(Path(args.env).expanduser().with_name('discord-setup.json'), text + '\n')
    return 0 if result['connected'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
