"""Tell the user on Discord when the recruiter sheet sync keeps failing.

The dashboard runs this after the sync that ends each evaluation, passing that sync's result. One
failure says nothing, because the next evaluation syncs again. See docs/recruiter-sheet.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.applier_discord import DEFAULT_ENV, http_client, load_env, main_channel
from pipeline.progress_monitor import atomic_write

STATE_NAME = 'recruiter-sheet-sync-alert.json'
ALERT_AFTER_FAILURES = 2
REASON_LENGTH = 300
LOG_COMMAND = "journalctl --user -u jobfeed-dashboard.service -g 'recruiter.sheet' -n 50"
RECOVERED = 'The recruiter sheet sync is working again. The Google Sheet is current and your edits are reaching the pipeline.'
NO_REASON = 'the sync gave no reason'
HEALTHY = {'consecutive_failures': 0, 'alert_outstanding': False}


def _log(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def _read_state(path):
    """Missing or corrupt state counts as healthy with no failures."""
    try:
        state = json.loads(path.read_text())
        failures, outstanding = state['consecutive_failures'], state['alert_outstanding']
    except (OSError, ValueError, KeyError, TypeError):
        return dict(HEALTHY)
    if type(failures) is not int or failures < 0 or type(outstanding) is not bool:
        return dict(HEALTHY)
    return {'consecutive_failures': failures, 'alert_outstanding': outstanding}


def _clean(text):
    return ' '.join(text.replace('`', '').split())[:REASON_LENGTH] or NO_REASON


def _failure_message(failures, reason):
    count = 'twice' if failures == ALERT_AFTER_FAILURES else f'{failures} times'
    return (f"The recruiter sheet sync has failed {count} in a row. The Google Sheet may be stale and your edits may not be reaching "
            f"the pipeline. Autopilot's hold still protects jobs on the sheet.\n```\n{reason}\n```\nLogs: `{LOG_COMMAND}`")


def _message(state, succeeded, reason):
    """The message this run owes, if any. The count in state is already up to date."""
    if succeeded:
        return RECOVERED if state['alert_outstanding'] else None
    if state['consecutive_failures'] >= ALERT_AFTER_FAILURES and not state['alert_outstanding']:
        return _failure_message(state['consecutive_failures'], _clean(reason))
    return None


def _connect(env_path):
    env = load_env(env_path)
    http = http_client(env['DISCORD_BOT_TOKEN'])
    channel, _ = main_channel(env, http)
    return http, channel


def run(root, succeeded, reason, *, connect):
    """Record one finished sync and post what it owes. A failed post leaves the alert owed, so the next sync tries again."""
    path = Path(root) / '.pipeline' / STATE_NAME
    state = _read_state(path)
    state['consecutive_failures'] = 0 if succeeded else state['consecutive_failures'] + 1
    message = _message(state, succeeded, reason)
    code = 0
    if message is not None:
        try:
            http, channel = connect()
            http('POST', f'/channels/{channel}/messages', {'content': message, 'allowed_mentions': {'parse': []}})
        except Exception as exc:
            _log('alert_post_failed', error=f'{type(exc).__name__}: {str(exc)[:200]}')
            code = 1
        else:
            state['alert_outstanding'] = not succeeded
            _log('alert_posted', kind='recovery' if succeeded else 'failure')
    atomic_write(path, json.dumps(state) + '\n')
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--env', type=Path, default=DEFAULT_ENV)
    parser.add_argument('--result', required=True, choices=['success', 'failure'])
    parser.add_argument('--reason', default='', help='why a failed sync failed, in one line')
    args = parser.parse_args(argv)
    return run(args.root, args.result == 'success', args.reason, connect=lambda: _connect(args.env))


if __name__ == '__main__':
    raise SystemExit(main())
