from __future__ import annotations

import json
import sys

from pipeline.coordination import CoordinationStore


def register(commands):
    parser = commands.add_parser('resources', help='Record job tabs and coordinate shared workflows')
    parser.add_argument('--scope', required=True)
    parser.add_argument('method', choices=('snapshot', 'configure-account', 'bind', 'observe', 'connect-tab', 'acquire', 'release', 'uncertain', 'reconcile'))
    parser.add_argument('--job-id', type=int)
    parser.add_argument('--owner')
    parser.add_argument('--controller')
    parser.add_argument('--dashboard-url')


def execute(config, args):
    store = CoordinationStore(config.resume_root, scope=args.scope, jobfeed_db=config.jobfeed_db)
    if args.method == 'snapshot':
        return store.snapshot([args.job_id] if args.job_id is not None else None)
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        raise ValueError('stdin must contain one JSON object')
    if args.method == 'connect-tab':
        if args.job_id is None or not args.owner:
            raise ValueError('--job-id and original --owner required')
        from pipeline.application_profile import connect_recorded_tab
        return connect_recorded_tab(config, args.scope, args.job_id, args.owner, data, args.dashboard_url)
    if args.method == 'configure-account':
        if not args.controller:
            raise ValueError('--controller required')
        return store.configure_account(args.controller, profile_id=data['profile_id'],
                                       tenant_origin=data['tenant_origin'], account_alias=data['account_alias'],
                                       job_ids=data['job_ids'])
    if args.method in ('bind', 'observe', 'acquire'):
        if args.job_id is None or not args.owner:
            raise ValueError('--job-id and original --owner required')
        if args.method == 'bind':
            return store.bind(args.job_id, args.owner)
        if args.method == 'observe':
            return store.observe_tabs(args.job_id, args.owner, data['tabs'], data['expected_revision'])
        return store.acquire(args.job_id, args.owner, data['kind'], data['resource_key'],
                             data['request_id'], data['owner_token'])
    if args.method == 'reconcile':
        if not args.controller:
            raise ValueError('--controller required')
        return store.reconcile(data['claim_id'], args.controller, data['evidence'], data['expected_revision'])
    method = store.release if args.method == 'release' else store.mark_uncertain
    return method(data['claim_id'], data['token'], data['evidence'])


def task_contract(root, scope, job_id, owner=None):
    import shlex
    from pathlib import Path
    python = Path(root) / '.venv/bin/python'
    command = f'{shlex.quote(str(python if python.exists() else Path(sys.executable)))} -m pipeline.cli --root {shlex.quote(str(root))} resources --scope {shlex.quote(str(scope))}'
    from pipeline.tab_preservation import TAB_PRESERVATION_RULE
    return {
        'tab_preservation': TAB_PRESERVATION_RULE,
        'job_id': job_id, 'owner': owner or 'use this original Codex chat ID',
        'command': command, 'cwd': str(root),
        'instructions': [
            'Before browser recovery read snapshot --job-id JOB. Reuse only exact browser/tab IDs recorded for this job and confirmed by official harness tools. URL matches do not prove ownership.',
            'After opening or observing a job tab or popup, record observe --job-id JOB --owner CHAT with stdin {"expected_revision":REV,"tabs":[{"browser_id":"BROWSER","tab_id":"TAB","opener_tab_id":null,"url":"URL","state":"observed"}]}. Read binding revision from snapshot. Record preserved, missing or closed observations after verification. Never adopt a foreign or missing tab automatically.',
            'After recording the actual form tab, and before each final deliverable whenever its dashboard binding was lost or the actual final URL changed, run connect-tab --job-id JOB --owner CHAT. stdin is {"candidates":[{"browser_id":"fresh CUA browser ID","extension_instance_id":"fresh CUA metadata.extensionInstanceId","profile_name":"observed profile","native_tab_id":123,"url":"exact current URL"}],"target":{"browser_id":"same browser ID","native_tab_id":123,"url":"same exact URL"}}. Use fresh official CUA inventory and the current full URL after the final readback or submission navigation, never an old pre-submit URL or guessed metadata. The command uses the registered dashboard origin; --dashboard-url may specify the current observed local dashboard origin. It probes and connects the existing tab without navigation, creation or manual pairing codes. On failure leave the form open, report the failed jump connection and still call markDeliverable; do not block authorized application work or fabricate a connection.',
            'Before login, registration, password or logout changes, read snapshot accounts for this job. Use only the controller-registered resource_key matching the observed browser profile, tenant origin and confirmed account alias. Never invent an alias. Missing or ambiguous account registration waits for controller configuration. Acquire account_mutation spanning observation, correlation, action and verification. Native authentication also requires native_workflow keyed by app bundle ID. Acquire account first and app second, release app before account, and never nest another account while holding an app claim. No credentials or OTPs in keys or records.',
            'acquire stdin requires kind, resource_key, request_id and a caller-generated random owner_token retained privately before invoking. Keys and request IDs allow letters, numbers, dot, underscore, colon and hyphen only. A waiting result means do not mutate that resource. For any shared native workflow acquire native_workflow with the app bundle ID as key, and correlate the exact app, tenant and observed challenge with this job before acting.',
            'release stdin requires claim_id, token and evidence {"status":"completed","observation_id":"SAVED-OBSERVATION"}. On timeout, crash uncertainty or ambiguous outcome use uncertain with reason and observation_id. Never release or take over because elapsed time freed a capacity slot. Only controller reconciliation can clear uncertainty.',
            'Claims coordinate participating pipeline actors. They do not intercept arbitrary CUA calls. Codex continues to enforce active tab ownership. Native focus or clipboard work must use participating adapters; do not bypass them with desktop input.',
        ],
    }


def prepare_owner(config, *, scope, job_id, owner):
    import sqlite3
    from contextlib import closing
    from pipeline.progress_monitor import atomic_write

    with closing(sqlite3.connect(config.jobfeed_db.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT application_folder_relpath,codex_application_session_id FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
    if row:
        if row[1] != owner or not row[0]:
            raise ValueError('verified original owner and job folder required')
        relative_folder = row[0]
    else:
        closeout = json.loads((config.resume_root / '.pipeline/submission-closeouts' / f'{job_id}.json').read_text())
        if closeout.get('job_id') != job_id or closeout.get('thread_id') != owner:
            raise ValueError('verified original closeout owner required')
        relative_folder = closeout['folder']
    folder = (config.applications_root / relative_folder).resolve()
    if not folder.is_relative_to(config.applications_root.resolve()) or not folder.is_dir():
        raise ValueError('original job folder unavailable within applications')
    CoordinationStore(config.resume_root, scope=scope, jobfeed_db=config.jobfeed_db).bind(job_id, owner)
    path = folder / 'resource-contract.json'
    atomic_write(path, json.dumps(task_contract(config.resume_root, scope, job_id, owner), ensure_ascii=False, indent=2) + '\n')
    return path
