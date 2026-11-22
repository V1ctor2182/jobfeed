"""PreToolUse hook for Claude Appliers: block browser tool calls that reach the local Dashboard.

The Dashboard is where the user confirms Submit, so an Applier must never open it. Claude Code runs this script
before every Claude in Chrome tool call with the call as JSON on standard input. Exit code 2 blocks the call.
It uses only the standard library, so it runs without the pipeline on the path.

The launcher passes the Dashboard's own addresses as the first argument, because a server install serves it on
another port than this machine's 8765 (2026-11-22: a 7654 Dashboard was open to every Applier).
"""
import json
import os
import sys
from urllib.parse import urlsplit

DEFAULT_BLOCKED = '127.0.0.1:8765,localhost:8765,[::1]:8765,jobfeed.localhost'


LOOPBACK = ('127.0.0.1', 'localhost', '[::1]', '0.0.0.0')


def dashboard_hosts(url):
    """How a browser on this machine reaches the Dashboard at url: every loopback name with its port, and its own host."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    return [*(f'{name}:{port}' for name in LOOPBACK), parts.netloc.lower()]


def blocked_hosts(environ=os.environ, extra=''):
    hosts = [*environ.get('APPLIER_BLOCKED_HOSTS', DEFAULT_BLOCKED).split(','), *extra.split(',')]
    return list(dict.fromkeys(h.strip().lower() for h in hosts if h.strip()))


def verdict(event, hosts):
    text = json.dumps(event.get('tool_input', {}), ensure_ascii=False).lower()
    hit = next((host for host in hosts if host in text), None)
    return None if hit is None else f'Blocked: Appliers never open the local Dashboard ({hit}). Submit is confirmed by the user there.'


def main(argv=sys.argv):
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print('Blocked: the guard could not read the tool call.', file=sys.stderr)
        return 2
    reason = verdict(event, blocked_hosts(extra=argv[1] if len(argv) > 1 else ''))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
