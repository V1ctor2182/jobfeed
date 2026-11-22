"""Check that Claude Appliers can start on this machine. Read-only: it starts nothing and posts nothing.

    python -m pipeline.applier_check

Prints one JSON object with a list of checks, each `ok` with a `fix` when it is not. Exit code 1 when any check fails.
It covers what the Dashboard's Start with Claude needs (profile, résumés, the claude and jobfeed commands, Claude's
trust in this folder, the Discord bot) and what an Applier needs once it runs (the applying browser with Claude in Chrome,
its recorded deviceId, the Workday account program and its password).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys
from types import SimpleNamespace

from pipeline.applier_discord import DEFAULT_ENV
from pipeline.claude_applier import APPLY_BROWSER, _DEVICE_ID
from pipeline.claude_dashboard_start import claude_trusts
from pipeline.config import PipelineConfig
from pipeline.workday_accounts import PASSWORD_FILE

PROFILE = 'Fill in My Profile on the dashboard, or ask the agent to fill it from your résumé.'


def _need(found, fix):
    return (True, 'found') if found else (False, fix)


def _profile(root):
    from jobfeed.my_profile_document import PersonalProfileStore
    state = PersonalProfileStore(root / 'application-profile.md', root.parent / 'jobfeed/data/my-profile-review.json').state()
    details = state.personal_details
    missing = [name for name in ('legal_name', 'primary_email', 'phone', 'work_authorization') if not getattr(details, name)]
    if missing:
        return False, f"My Profile is missing {', '.join(missing)}. {PROFILE}"
    return True, f'{details.legal_name}, {details.primary_email}'


def _resumes(root):
    from pipeline.resume_catalog import ResumeCatalog
    catalog = ResumeCatalog.load(root / 'resume-catalog.json')
    return True, ', '.join(entry['label'] for entry in catalog.entries)


def _discord(env_path, client):
    from pipeline import discord_setup
    if not Path(env_path).expanduser().is_file():
        return False, 'Discord is not connected. Run python -m pipeline.discord_setup.'
    args = SimpleNamespace(check=True, again=False, env=str(env_path), server=None)
    try:
        state = discord_setup.run(args, **({'client': client} if client else {}))
    except discord_setup.Problem as exc:
        return False, exc.fix
    return True, f"bot {state['bot']} in {state['server']}"


def _browser(targets):
    from pipeline.apply_browser import extension_running
    if not extension_running(targets()):
        return False, ('Claude in Chrome is not running in the applying browser. Open that browser, install the Claude '
                       'extension and sign in to it with your Claude account.')
    return True, 'Claude in Chrome is running'


def _device(root):
    try:
        device = json.loads((root / APPLY_BROWSER).read_text()).get('device_id')
    except (OSError, ValueError, AttributeError):
        device = None
    if not (isinstance(device, str) and _DEVICE_ID.fullmatch(device)):
        return False, f'Record the applying browser\'s Claude in Chrome deviceId in {APPLY_BROWSER}.'
    return True, device


def _broker(root):
    from pipeline.browser_broker import socket_path
    with socket_path(root) as path, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.connect(path)
        except OSError:
            return False, 'The Workday account program is not running: systemctl --user start jobfeed-browser-broker.'
    return True, 'running'


def checks(root, *, env=os.environ, discord_env=DEFAULT_ENV, claude_config=Path('~/.claude.json'), targets=None, discord_client=None):
    from pipeline.apply_browser import _targets
    root = Path(root).resolve()
    path = os.pathsep.join([str(Path(sys.executable).parent), env.get('PATH', '')])
    items = [
        ('My Profile', lambda: _profile(root)),
        ('Résumés for applications', lambda: _resumes(root)),
        ('claude command', lambda: _need(shutil.which('claude', path=path), 'Install Claude Code and sign it in.')),
        ('jobfeed command', lambda: _need(os.access(root.parent / 'jobfeed/.venv/bin/jobfeed', os.X_OK),
                                          'Jobfeed is not installed beside the pipeline.')),
        ('Claude trusts this folder', lambda: _need(claude_trusts(root / 'applications', Path(claude_config).expanduser()),
                                                    f'Run claude once in {root.parent} and accept the trust question.')),
        ('Same Jobfeed database', lambda: _need(PipelineConfig.discover(root).jobfeed_db.is_file(),
                                                'The Jobfeed database is not at jobfeed/data/jobfeed.sqlite.')),
        ('Discord', lambda: _discord(discord_env, discord_client)),
        ('Applying browser', lambda: _browser(targets or _targets)),
        ('Applying browser recorded', lambda: _device(root)),
        ('Workday account program', lambda: _broker(root)),
        ('Job-site password', lambda: _need((root / PASSWORD_FILE).is_file(),
                                            'Set a password under Login on the dashboard\'s My Profile page.')),
    ]
    results = []
    for name, check in items:
        try:
            ok, detail = check()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            ok, detail = False, f'{type(exc).__name__}: {exc}'[:300]
        results.append({'name': name, 'ok': bool(ok), **({'detail': detail} if ok else {'fix': detail})})
    return results


def main(argv=None, **kwargs):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    results = checks(args.root, **kwargs)
    print(json.dumps({'ready': all(r['ok'] for r in results), 'checks': results}, indent=2, ensure_ascii=False))
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
