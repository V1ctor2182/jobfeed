"""Drain Claude Appliers, or let them start again, without replaying or transferring work.

`drain` stops new launches and stops the Appliers waiting at a safe point. Every Claude-owned job keeps its owner.
Run it again until `working` is empty. `resume` lets new launches start again.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.claude_applier import ClaudeApplierLauncher
from pipeline.claude_dispatch import is_claude_owner


def claude_jobs(jobfeed_db):
    from pipeline.store import ApplicationQueue
    return [{'job_id': item.job_id, 'state': item.state.value, 'owner': item.codex_application_session_id}
            for item in ApplicationQueue(jobfeed_db).list_active() if is_claude_owner(item.codex_application_session_id)]


def status(root, jobfeed_db):
    launcher = ClaudeApplierLauncher(root)
    return {'draining': launcher.store.control('draining') is not None,
            'auto_submit': launcher.store.auto_submit(), 'claude_jobs': claude_jobs(jobfeed_db)}


def drain(root, jobfeed_db):
    return {'drain': ClaudeApplierLauncher(root).drain(), 'claude_jobs_kept': claude_jobs(jobfeed_db),
            'note': 'No job was replayed or lost its owner. Run drain again until drain.working is empty.'}


def resume(root):
    ClaudeApplierLauncher(root).resume_dispatch()
    return {'draining': False}


def main(argv=None):
    from pipeline.config import PipelineConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'drain', 'resume'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args(argv)
    config = PipelineConfig.discover(args.root)
    if args.command == 'status':
        result = status(config.resume_root, config.jobfeed_db)
    elif args.command == 'drain':
        result = drain(config.resume_root, config.jobfeed_db)
    else:
        result = resume(config.resume_root)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
