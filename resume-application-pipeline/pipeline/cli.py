"""Command-line entry point for the deterministic resume pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pipeline.config import PipelineConfig
from pipeline.orchestrator import ApplicationPipeline
from pipeline.worker import OrchestratorWorker, WorkerSettings


def _json(value: Any) -> str:
    return json.dumps(value, default=str, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    from pipeline.resource_cli import register as register_resources
    register_resources(commands)
    commands.add_parser("init")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("job_ids", type=int, nargs="+")
    watch = commands.add_parser("watch")
    watch.add_argument("--poll-interval", type=float, default=1.0)
    watch.add_argument("--queue-target", type=int, default=100)
    watch.add_argument("--application-workers", type=int, default=4)
    watch.add_argument("--batch", type=Path, required=True)
    watch.add_argument('--worker-child', action='store_true', help=argparse.SUPPRESS)
    watch.add_argument("--progress-output", type=Path, help="Enable verified progress reporting; outside business state")
    watch.add_argument("--progress-local-only", action="store_true", help="Explicitly allow reports without chat notification delivery")
    cleanup = commands.add_parser('finish-submission')
    cleanup.add_argument('job_id', type=int)
    email = commands.add_parser('record-confirmation-email', help='Save a raw RFC 822 confirmation email as the job\'s evidence')
    email.add_argument('job_id', type=int)
    email.add_argument('--eml', type=Path, required=True, help='The original message bytes, as the mail tool returned them')
    email.add_argument('--source-url', required=True, help='The message permalink in the mail tool')
    email.add_argument('--capture-tool', required=True, help='The mail tool and operation that fetched it')
    commands.add_parser("status")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id", type=int)
    requeue = commands.add_parser('requeue', help='Put stranded Resume Ready or Applying jobs back in the queue, retiring their ended Claude Applier')
    requeue.add_argument('job_ids', type=int, nargs='+')
    requeue.add_argument('--reason', required=True, help='Why the user is putting these jobs back; kept in the requeue record')
    requeue.add_argument('--dry-run', action='store_true', help='Run every check and print the plan without writing')
    requeue.add_argument('--unknown-sent-nothing', action='store_true',
                         help='The user states that, for every listed job, a Submit attempt recorded as unknown sent nothing, such as a form '
                              'that stopped on an emailed code nobody entered. Never for a page that acknowledged the application')
    requeue.add_argument('--keep-batches', action='store_true', help='Leave the jobs in every batch that lists them, so that batch starts them again. By default those batches stop claiming them')
    auto_submit = commands.add_parser('auto-submit', help='Let the system confirm Submit for Claude Applier jobs ready for review. Only the user turns it on')
    auto_submit.add_argument('mode', choices=('on', 'off', 'status'))
    autopilot = commands.add_parser('autopilot', help='Standing authorization to scan, evaluate and apply to eligible jobs. Only the user turns it on; anyone turns it off')
    autopilot.add_argument('mode', choices=('on', 'off', 'status'))
    autopilot.add_argument('--by', help='Who turns it off, kept in the record')
    from pipeline.workday_accounts import register as register_accounts
    register_accounts(commands)
    captcha = commands.add_parser('captcha', help='List jobs set aside as CAPTCHA needed, or clear them once you solved the check in Chrome')
    captcha_commands = captcha.add_subparsers(dest='captcha_command', required=True)
    captcha_commands.add_parser('list')
    captcha_commands.add_parser('clear').add_argument('job_ids', type=int, nargs='+')
    args = parser.parse_args()

    if args.command == 'resources':
        config = PipelineConfig.discover(args.root)
        try:
            from pipeline.resource_cli import execute
            result = execute(config, args)
        except (OSError, ValueError, KeyError) as exc:
            print(_json({'error': str(exc)}), file=sys.stderr)
            return 2
        print(_json(result))
        return 0

    if args.command == 'requeue':
        if not args.reason.strip():
            parser.error('--reason must say why these jobs go back to the queue')
        from pipeline.requeue import requeue
        results = requeue(PipelineConfig.discover(args.root), args.job_ids, args.reason.strip(), dry_run=args.dry_run, keep_batches=args.keep_batches,
                          unknown_sent_nothing=args.unknown_sent_nothing)
        print(_json(results))
        return 1 if any(result['status'] == 'refused' for result in results) else 0

    if args.command == 'auto-submit':
        from pipeline.applier_runtime import ApplierRuntime
        runtime = ApplierRuntime(PipelineConfig.discover(args.root).resume_root)
        if args.mode != 'status':
            runtime.set_auto_submit(args.mode == 'on', actor='user via pipeline.cli auto-submit', source='pipeline.cli auto-submit')
        print(_json(runtime.auto_submit()))
        return 0
    if args.command == 'autopilot':
        if args.by and args.mode != 'off':
            parser.error('--by applies to off only; autopilot is turned on by the user')
        from pipeline.applier_runtime import ApplierRuntime
        runtime = ApplierRuntime(PipelineConfig.discover(args.root).resume_root)
        if args.mode == 'on':
            runtime.set_autopilot(True, actor='user via pipeline.cli autopilot', source='pipeline.cli autopilot on')
        elif args.mode == 'off':
            runtime.set_autopilot(False, actor=args.by or 'pipeline.cli autopilot off', source='pipeline.cli autopilot off')
        print(_json(runtime.autopilot()))
        return 0
    if args.command == 'accounts':
        from pipeline.workday_accounts import PasswordError, execute as accounts
        try:
            print(_json(accounts(PipelineConfig.discover(args.root).resume_root, args)))
        except PasswordError as exc:
            print(_json({'error': str(exc)}), file=sys.stderr)
            return 2
        return 0
    if args.command == 'captcha':
        from pipeline.captcha_holds import clear_jobs, held
        config = PipelineConfig.discover(args.root)
        results = held(config) if args.captcha_command == 'list' else clear_jobs(config, args.job_ids)
        print(_json(results))
        return 1 if any(result.get('status') == 'not_held' for result in results) else 0

    if args.command == 'watch' and args.progress_output and not args.progress_local_only:
        parser.error('--progress-output requires --progress-local-only')
    if args.command == 'watch' and args.progress_output and not args.worker_child:
        from pipeline.worker_supervisor import supervise
        result = supervise(PipelineConfig.discover(args.root).resume_root, args.batch, args.progress_output,
                           [sys.executable, '-m', 'pipeline.cli', *sys.argv[1:], '--worker-child'])
        print(_json({'supervisor': result}))
        return 0
    if args.command == "watch":
        from pipeline.batch import BatchPipeline
        pipeline = BatchPipeline(PipelineConfig.discover(args.root), args.batch)
        args.application_workers = min(args.application_workers, pipeline.batch_limit)
    else:
        pipeline = ApplicationPipeline(PipelineConfig.discover(args.root))
        pipeline.initialize()
    if args.command == "init":
        print(_json({"initialized": True, "db": pipeline.config.jobfeed_db}))
    elif args.command == "enqueue":
        print(_json({job_id: pipeline.enqueue(job_id) for job_id in args.job_ids}))
    elif args.command == "watch":
        progress_monitor = None
        if args.progress_output:
            from pipeline.progress_monitor import ProgressMonitor, LocalSources
            progress_monitor = ProgressMonitor(LocalSources(pipeline.config.resume_root, args.batch,
                pipeline.config.jobfeed_db), args.progress_output)
        OrchestratorWorker(
            pipeline,
            WorkerSettings(
                queue_target=args.queue_target,
                application_workers=args.application_workers,
                poll_interval=args.poll_interval,
            ),
            progress_monitor=progress_monitor,
        ).run_forever()
    elif args.command == 'finish-submission':
        print(_json(pipeline.finish_submission(args.job_id)))
    elif args.command == 'record-confirmation-email':
        from pipeline.confirmation_email import record
        print(_json(record(pipeline, args.job_id, args.eml.read_bytes(), source_url=args.source_url, capture_tool=args.capture_tool)))
    elif args.command == "status":
        print(_json({"applications": [asdict(item) for item in pipeline.queue.list_active()],
                     "advice": pipeline.queue.list_advice()}))
    elif args.command == "cancel":
        pipeline.cancel(args.job_id)
        print(_json({"cancelled": args.job_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
