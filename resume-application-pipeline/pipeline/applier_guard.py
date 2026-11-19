"""PreToolUse hook for Claude Appliers: block browser tool calls that reach the local Dashboard.

The Dashboard is where the user confirms Submit, so an Applier must never open it. Claude Code runs this script
before every Claude in Chrome tool call with the call as JSON on standard input. Exit code 2 blocks the call.
It uses only the standard library, so it runs without the pipeline on the path.
"""
import json
import os
import sys

DEFAULT_BLOCKED = '127.0.0.1:8765,localhost:8765,[::1]:8765,jobfeed.localhost'


def blocked_hosts(environ=os.environ):
    return [h.strip().lower() for h in environ.get('APPLIER_BLOCKED_HOSTS', DEFAULT_BLOCKED).split(',') if h.strip()]


def verdict(event, hosts):
    text = json.dumps(event.get('tool_input', {}), ensure_ascii=False).lower()
    hit = next((host for host in hosts if host in text), None)
    return None if hit is None else f'Blocked: Appliers never open the local Dashboard ({hit}). Submit is confirmed by the user there.'


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print('Blocked: the guard could not read the tool call.', file=sys.stderr)
        return 2
    reason = verdict(event, blocked_hosts())
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
