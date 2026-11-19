"""Explicit dashboard replacement of one stopped application's Desktop owner."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from uuid import UUID

from pipeline import application_rerun as rerun
from pipeline.cancellation import utc_now
from pipeline.coordination import CoordinationStore, _origin

ACTIVE_PHASES = {'relay_pending', 'cleanup', 'create_unknown', 'candidate_received', 'adopting', 'adopted', 'filling'}


def is_archived(owner, project):
    from pipeline.codex_app_server import CodexAppServer
    cursor = None
    with CodexAppServer(approve_requests=False) as server:
        for _ in range(10):
            args = {'archived': True, 'limit': 100, 'sortKey': 'updated_at'}
            if cursor:
                args['cursor'] = cursor
            page = server.request('thread/list', args)
            for thread in page['data']:
                if thread.get('id') == owner:
                    return thread.get('cwd') == project and thread.get('originator') == 'Codex Desktop'
            cursor = page.get('nextCursor')
            if not cursor:
                return False
    raise rerun.RerunError('Old chat archival could not be verified.', 'unavailable')


def verified_preparation(thread, folder, job, project, reporting_turn):
    """A current completed pre-submit observation can explain a generic verification result."""
    try:
        proof = rerun._object(folder / 'pre-submit-result.json')
        if (proof.get('job_id') != job.id or proof.get('job_url') != job.url
                or proof.get('submit_attempted') is not False or proof.get('submit_clicked') is not False
                or proof.get('status') not in {'pre_submit_draft_incomplete', 'pre_submit_ready'}):
            return False
        observed = datetime.fromisoformat(proof['recorded_at'])
        if observed.tzinfo is None or observed > datetime.now(timezone.utc):
            return False
        from pipeline.claude_codex_bridge import read_turn_lifecycle
        for turn in reversed(thread['turns']):
            if turn['id'] != reporting_turn:
                continue
            lifecycle = read_turn_lifecycle(thread.get('path'), thread['id'], turn['id'], project)
            if lifecycle.get('state') != 'completed' or not lifecycle.get('timestamp'):
                continue
            ended = datetime.fromisoformat(lifecycle['timestamp'].replace('Z', '+00:00'))
            starts = []
            with Path(thread['path']).open() as handle:
                for line in handle:
                    event = json.loads(line)
                    payload = event.get('payload', {})
                    if event.get('type') == 'event_msg' and payload.get('type') == 'task_started' and payload.get('turn_id') == turn['id']:
                        starts.append(datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')))
            if len(starts) == 1 and starts[0] <= observed <= ended:
                return True
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return False


class Restarts:
    def __init__(self, service):
        self.service, self.root, self.gate = service, service.root, service.gate

    def row(self, request_id):
        with self.gate._db() as db:
            row = db.execute('SELECT r.*,s.phase,s.candidate_owner,s.target_turn,s.details FROM application_reruns r JOIN application_restarts s USING(request_id) WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            raise rerun.RerunError('Unknown application restart.')
        return dict(row)

    def public(self, row):
        return rerun._public(row) | {'phase': row['phase'], 'replacement_thread_id': row['candidate_owner']}

    def command(self, request_id, operation):
        return shlex.join([str(self.root / '.venv/bin/python'), '-m', 'pipeline.application_restart', '--root', str(self.root), '--jobfeed-db', str(self.service.jobfeed_db), operation, request_id])

    def guard(self, row):
        if row['state'] not in rerun.OPEN_STATES:
            raise rerun.RerunError('This restart is no longer active.')
        with self.gate._db() as db:
            current = db.execute('SELECT state,stops FROM application_reruns WHERE request_id=?', (row['request_id'],)).fetchone()
            if current['state'] not in rerun.OPEN_STATES or rerun.stop_snapshot(db, row['scope'], row['job_id']) != json.loads(current['stops']):
                raise rerun.RerunError('A newer stop revoked this restart.')
        details = json.loads(row['details'])
        item = rerun.ApplicationQueue(self.service.jobfeed_db).get(row['job_id'])
        if not item or item.codex_application_session_id not in {row['owner_thread_id'], row['candidate_owner']} - {None}:
            raise rerun.RerunError('The application owner changed outside this restart.')
        if str(item.state) not in rerun.PREPARATION_STATES or rerun.ApplicationQueue(self.service.jobfeed_db).paused(row['job_id']):
            raise rerun.RerunError('The application outcome or review state changed.')
        jobfeed = rerun.Jobfeed(self.service.jobfeed_db, self.root.parent / 'jobfeed')
        job = jobfeed.get_job(row['job_id'], require_open=True)
        if job.url != details['url'] or jobfeed.is_applied(row['job_id']):
            raise rerun.RerunError('The application URL or submission state changed.')
        folder = rerun._confined(self.root / 'applications', Path(details['folder']))
        for name in ('submission-receipt.json', 'user-submission-confirmation.json', 'confirmation-page.json'):
            if (folder / name).exists():
                raise rerun.RerunError('Submission evidence requires verification before further work.')
        if (self.root / '.pipeline/submission-closeouts' / f'{row["job_id"]}.json').exists():
            raise rerun.RerunError('Submission closeout work requires verification.')
        from pipeline.results import parse_application_result
        for owner in {row['owner_thread_id'], row['candidate_owner']} - {None}:
            thread = rerun.read_thread(owner)
            for turn in thread['turns']:
                for message in turn.get('items', []):
                    if message.get('type') != 'agentMessage':
                        continue
                    try:
                        result = parse_application_result(message.get('text', ''))
                    except (ValueError, TypeError):
                        continue
                    if result.get('status') in {'submitted', 'submission_unknown', 'already_applied', 'unavailable'} or result.get('success_evidence'):
                        raise rerun.RerunError('The applier reported a terminal or uncertain application outcome.')
        manifest = rerun._object(Path(row['scope']))
        handoff = rerun._object(Path(row['scope']).with_name(Path(row['scope']).stem + '-handoff.json'))
        members = [item.get('id') for item in manifest.get('ordered_candidates', []) if isinstance(item, dict)]
        if (manifest.get('controller_thread_id') != row['controller'] or row['job_id'] not in members
                or handoff.get('status') != 'stopped_by_user' or handoff.get('manifest') != row['scope']
                or handoff.get('controller') != row['controller']):
            raise rerun.RerunError('The stopped batch changed.')
        with rerun.closing(rerun._readonly(self.root / '.pipeline/blockers.sqlite3')) as db:
            scope = db.execute('SELECT controller,active FROM dq_scopes WHERE scope=?', (row['scope'],)).fetchone()
            member = db.execute('SELECT state FROM dq_jobs WHERE scope=? AND job=?', (row['scope'], str(row['job_id']))).fetchone()
        if not scope or scope != (row['controller'], 0) or not member or member[0] != 'active':
            raise rerun.RerunError('The stopped scope or job membership changed.')
        return details

    def relay_turn(self, row, *, active=True):
        details = self.guard(row)
        thread = rerun.read_thread(row['owner_thread_id'])
        if thread.get('originator') != 'Codex Desktop' or Path(thread.get('cwd', '')).resolve() != Path(details['project']):
            raise rerun.RerunError('The original Desktop relay changed.')
        turns = [turn for turn in thread['turns'] if rerun._matches(turn, row)]
        if len(turns) != 1 or row['turn_id'] not in {None, turns[0]['id']}:
            raise rerun.RerunError('The exact restart relay turn is not verified.', 'unavailable')
        state = rerun.host_turn_state(thread, turns[0]['id'], details['project'])
        if active and state != 'active':
            raise rerun.RerunError('The restart relay is no longer active.')
        with self.gate._db() as db:
            db.execute('UPDATE application_reruns SET turn_id=? WHERE request_id=? AND state IN (\'unknown\',\'queued\',\'running\')', (turns[0]['id'], row['request_id']))
        return thread, turns[0], state

    def target(self, row):
        details = self.guard(row)
        from pipeline.claude_codex_bridge import creation_turn
        thread = rerun.read_thread(row['candidate_owner'])
        turn = creation_turn({'thread': thread}, {'prompt': details['create_arguments']['prompt'], 'created_at': row['created_at']}, {'project_root': details['project'], 'relay': row['owner_thread_id']}, row['candidate_owner'])
        if row['target_turn'] not in {None, turn['id']}:
            raise rerun.RerunError('The replacement initial turn changed.')
        return thread, turn

    def status(self, row):
        row = self.row(row['request_id'])
        owner = rerun.ApplicationQueue(self.service.jobfeed_db).get(row['job_id']).codex_application_session_id
        eligible, reason = False, row['reason']
        if row['state'] in rerun.OPEN_STATES:
            reason = reason or 'The application restart is in progress. It will not be sent again.'
        elif row['phase'] == 'completed':
            try:
                with rerun.worker_stopped(self.root):
                    self.service.context(row['batch_id'], row['job_id'])
                eligible, reason = True, None
            except Exception as exc:
                reason = str(exc)
        return {'eligible': eligible, 'reason': reason, 'owner_thread_id': owner, 'request': self.public(row)}

    def request(self, batch_id, job_id, request_id, expected_owner, dashboard_url=None):
        from pipeline.submission_reservation import admission_lock
        try:
            with admission_lock(self.root, blocking=False):
                return self._reserved_request(batch_id, job_id, request_id, expected_owner, dashboard_url)
        except BlockingIOError:
            raise rerun.RerunError('Another application admission is in progress.') from None

    def _reserved_request(self, batch_id, job_id, request_id, expected_owner, dashboard_url):
        if str(UUID(request_id)) != request_id:
            raise rerun.RerunError('A random UUID request ID is required.', 'invalid')
        from pipeline.submission_reservation import assert_available
        with rerun.worker_stopped(self.root):
            assert_available(self.root, job_id)
            with self.gate._db() as db:
                previous = db.execute('SELECT * FROM application_reruns WHERE request_id=?', (request_id,)).fetchone()
                pending = db.execute("SELECT * FROM application_reruns WHERE job_id=? AND state IN ('unknown','queued','running')", (job_id,)).fetchone()
            if previous:
                if (previous['batch_id'], previous['job_id'], previous['owner_thread_id']) != (batch_id, job_id, expected_owner):
                    raise rerun.RerunError('This request ID belongs to another application.')
                if previous['source'] != 'local_dashboard_restart':
                    return self.service._request(batch_id, job_id, request_id, expected_owner)
                return self.status(self.reconcile(dict(previous)))
            if pending:
                return self.service.status(batch_id, job_id)
            from pipeline.application_profile import ProfileBridge
            ProfileBridge(dashboard_url).available()
            context = self.service.context(batch_id, job_id, owner=expected_owner)
            from pipeline.resume_catalog import selected_materials
            selected_materials(context['folder'], job_id=job_id, scope=context['scope'], owner=expected_owner)
            manifest = rerun._object(Path(context['scope']))
            project_id = manifest.get('desktop_project_id')
            if not project_id:
                raise rerun.RerunError('The Desktop project registration is missing.')
            resources = CoordinationStore(self.root, scope=context['scope'], jobfeed_db=self.service.jobfeed_db).snapshot([job_id])
            if len(resources['bindings']) != 1 or resources['bindings'][0]['owner'] != expected_owner:
                raise rerun.RerunError('The original resource ownership is unavailable.')
            if any(claim['status'] != 'released' for claim in resources['claims']):
                raise rerun.RerunError('A shared browser or account resource needs reconciliation first.')
            contract = rerun._object(context['folder'] / 'resource-contract.json')
            tabs = [tab for tab in resources['tabs'] if tab['owner'] == expected_owner and tab['state'] not in {'missing', 'closed'}]
            from pipeline.session_names import applier_title
            replacement_prompt = (
                f'[application-restart:{request_id}]\nThe user explicitly requested a fresh application chat and form for '
                f'{context["job"].company}, {context["job"].title}, job {job_id}. You are its replacement applier. '
                f'Before any browser or resume action run {self.command(request_id, "await-adoption")} --owner "$CODEX_THREAD_ID". '
                'Poll that command until status is adopted. Never touch the form while waiting. A revoked request means stop. '
                f'Run pipeline commands with working directory {self.root}. If the shell sandbox blocks local Codex IPC, '
                'retry the same pipeline command with the normal require_escalated approval mechanism. Do not change its arguments or bypass a rejection. '
                f'After adoption read {context["folder"] / "application-task.json"} and resource-contract.json. '
                'Get fresh official CUA inventory before opening a tab. Run '
                f'{self.command(request_id, "verify-profile")} --owner "$CODEX_THREAD_ID" with the inventory candidate JSON on stdin. '
                'Each candidate has browser_id, extension_instance_id from the CUA browser metadata, profile_name, native_tab_id and exact url. '
                'The command verifies the actual Jobfeed extension, not merely the Codex extension. Use only its returned verified browser_id. '
                'On any failure stop and preserve all tabs. Never guess a profile or open a fallback tab. '
                f'Open a fresh Chrome application tab at {context["job"].url} only in that verified browser profile. '
                'Record its exact native tab ID, current CUA browser ID and URL through the resource contract. Then run '
                f'{self.command(request_id, "pair-tab")} --owner "$CODEX_THREAD_ID" with JSON browser_id, native_tab_id and url on stdin. '
                'Do not fill or upload anything until pair-tab confirms connected. This connects the dashboard jump to this exact form. '
                'The previous form was deliberately closed. Do not claim its unsaved state was restored. '
                'Reuse the exact selected resume. Immediately before upload use the resume verify command returned by await-adoption, '
                'including its restart-request and exact turn-id. Do not call select or change the resume. '
                'Fill confirmed facts and individually approved answer-bank versions. Leave missing facts and unapproved answers blank. '
                'STOP BEFORE SUBMIT regardless of older authorization. Never restart the batch or worker. '
                'Save pre-submit-result.json with job_id, job_url, recorded_at, submit_attempted:false, submit_clicked:false and status pre_submit_draft_incomplete or pre_submit_ready according to the actual preparation outcome. '
                'Record the new tab and call its official markDeliverable before finishing, again after every later read-only verification turn. A preserved resource record is not a browser mark. Keep the actual tab open after success too. Do not create or message other chats.')
            create_arguments = {'title': applier_title(manifest, context['job']), 'prompt': replacement_prompt,
                                'target': {'type': 'project', 'projectId': project_id, 'environment': {'type': 'local'}}}
            prompt = (
                f'[application-rerun:{request_id}]\nThe user explicitly requests restarting only job {job_id}, closing its previous form and replacing this applier chat. '
                'This turn is a cleanup and Desktop creation relay, not an application filling turn. STOP BEFORE SUBMIT. '
                'Do not fill, upload, submit, restart the worker or resume the batch. '
                f'Run pipeline commands with working directory {self.root}. If the shell sandbox blocks local Codex IPC, '
                'retry the same pipeline command with the normal require_escalated approval mechanism. Do not change its arguments or bypass a rejection. '
                f'Read {context["folder"] / "resource-contract.json"} and run {self.command(request_id, "inspect")} --owner "$CODEX_THREAD_ID". '
                'Before closing ANY tab, get fresh official CUA browser inventory and run '
                f'{self.command(request_id, "verify-profile")} --owner "$CODEX_THREAD_ID" with JSON candidates on stdin. '
                'Candidates are at most 16 existing Chrome tabs, sampled across every available Chrome profile, each with browser_id, '
                'extension_instance_id from CUA metadata.extensionInstanceId, profile_name, native_tab_id and exact url. '
                'The Jobfeed extension must confirm a unique profile. If no profile matches, stop before cleanup; do not open a test or replacement tab. '
                'The verified destination profile may differ from the old form profile. Browser IDs can change between turns. '
                'For cleanup match the saved native tab ID, exact URL and saved CUA profile before closing only the recorded job tabs. '
                'Never close another tab just because its URL matches. '
                'If a recorded tab is already absent, do not reopen it. Verify absence with the official global inventory and record closed or missing '
                'through the resource contract observe command using its saved coordination browser_id/tab_id keys, even when the current CUA browser ID changed. '
                'Use the fresh CUA ID only for browser control. If identity is ambiguous, stop and report it. '
                f'Then run {self.command(request_id, "prepare")} --owner "$CODEX_THREAD_ID". '
                'It returns frozen official create_thread arguments only once. Call that official Desktop tool exactly once. '
                f'Then run {self.command(request_id, "ack")} --owner "$CODEX_THREAD_ID" --target RETURNED_THREAD_ID. '
                'Repeat only ack if adoption is still pending. Never repeat create after an uncertain outcome. '
                'Only after ack returns adopted, archive this old applier chat with the official set_thread_archived tool. '
                'Leave the replacement chat active. These explicit instructions authorize this one replacement creation and old-chat archival; '
                'they override the normal executor prohibition on creating another application chat for this transaction only.')
            details = {'folder': str(context['folder']), 'project': str(context['project']), 'url': context['job'].url,
                       'tabs': tabs, 'profile': None, 'profile_guard_version': 1, 'dashboard_url': dashboard_url, 'create_arguments': create_arguments,
                       'previous_resource_contract': contract, 'previous_task': rerun._object(context['folder'] / 'application-task.json')}
            with self.gate._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if rerun.stop_snapshot(db, context['scope'], job_id) != context['stops']:
                    raise rerun.RerunError('The application stop changed.')
                reserved = {r[0] for r in db.execute("SELECT owner_thread_id FROM application_reruns WHERE scope=? AND state IN ('unknown','queued','running')", (context['scope'],))}
                if len(reserved | context['active_peers']) >= context['limit']:
                    raise rerun.RerunError('The controller has no free application capacity.')
                db.execute('INSERT INTO application_reruns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (request_id, batch_id, job_id, context['scope'], context['controller'], expected_owner, str(self.service.jobfeed_db), 'unknown', None, utc_now(), None, prompt, json.dumps([t['id'] for t in context['thread']['turns']]), json.dumps(context['stops']), json.dumps({'delivery_started': True}), 'local_dashboard_restart'))
                db.execute('INSERT INTO application_restarts VALUES(?,\'relay_pending\',NULL,NULL,?)', (request_id, json.dumps(details)))
            try:
                self.guard(self.row(request_id))
                receipt = rerun.send_existing(self.root, expected_owner, prompt) | {'delivery_started': True}
                with self.gate._db() as db:
                    db.execute("UPDATE application_reruns SET state='queued',receipt=? WHERE request_id=? AND state='unknown'", (json.dumps(receipt), request_id))
            except Exception:
                with self.gate._db() as db:
                    db.execute("UPDATE application_reruns SET reason='Restart delivery is uncertain. It will not be resent.' WHERE request_id=? AND state='unknown'", (request_id,))
            return self.status(self.row(request_id))

    def inspect(self, row, owner):
        if owner != row['owner_thread_id']:
            raise rerun.RerunError('Only the original applier may relay this restart.')
        details = self.guard(row)
        self.relay_turn(row)
        return {'status': row['phase'], 'tabs': details['tabs'], 'profile': details['profile'], 'resource_contract': str(Path(details['folder']) / 'resource-contract.json')}

    def profile_bridge(self, row):
        from pipeline.application_profile import ProfileBridge
        return ProfileBridge(json.loads(row['details']).get('dashboard_url'))

    def profile_actor(self, row, owner):
        if owner == row['owner_thread_id'] and row['phase'] in {'relay_pending', 'cleanup'}:
            self.inspect(row, owner)
            return 'relay'
        if owner == row['candidate_owner'] and row['phase'] in {'adopted', 'filling'}:
            self.await_adoption(row, owner)
            return 'replacement'
        raise rerun.RerunError('Only the current restart actor can verify its browser profile.')

    def verify_profile(self, row, owner, payload):
        from pipeline.application_profile import candidates
        stage = self.profile_actor(row, owner)
        inventory = candidates(payload)
        details = json.loads(row['details'])
        bridge = self.profile_bridge(row)
        previous = details.get('profile')
        if stage == 'replacement':
            if not previous:
                raise rerun.RerunError('The restart has no verified destination Chrome profile.')
            inventory = [item for item in inventory if item['extension_instance_id'] == previous['extension_instance_id']]
            if not inventory:
                raise rerun.RerunError('The verified Chrome profile is absent from the fresh CUA inventory.')
        proof = bridge.probe(inventory)
        matches = {(item['native_tab_id'], item['url']) for item in proof['matches']}
        found = [item for item in inventory if (item['native_tab_id'], item['url']) in matches]
        profiles = {(item['browser_id'], item['extension_instance_id']) for item in found}
        if len(profiles) != 1:
            raise rerun.RerunError('No unique CUA profile matches the connected Jobfeed extension. Keep the old form open.')
        profile = {key: found[0][key] for key in ('browser_id', 'extension_instance_id', 'profile_name')}
        profile.update(profile_proof=proof['profile_proof'], expires_at=proof['expires_at'], verified_owner=owner)
        details['profile'] = profile
        self.guard(row)
        with self.gate._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.same_observation(db, row):
                raise rerun.RerunError('The restart changed while checking Chrome. Inspect the same request again.')
            db.execute('UPDATE application_restarts SET details=? WHERE request_id=?', (json.dumps(details), row['request_id']))
        return {'status': 'profile_verified', **profile}

    def pair_tab(self, row, owner, payload):
        from pipeline.application_profile import tab_candidate
        if self.profile_actor(row, owner) != 'replacement':
            raise rerun.RerunError('Only the replacement applier may connect its new form.')
        target = tab_candidate(payload)
        details = json.loads(row['details'])
        profile = details.get('profile') or {}
        if profile.get('verified_owner') != owner or profile.get('browser_id') != target['browser_id']:
            raise rerun.RerunError('Verify the replacement Chrome profile before connecting its form.')
        snapshot = CoordinationStore(self.root, scope=row['scope'], jobfeed_db=self.service.jobfeed_db).snapshot([row['job_id']])
        if not any(tab['owner'] == owner and tab['browser_id'] == target['browser_id']
                   and str(tab['tab_id']) == str(target['native_tab_id']) and tab['origin'] == _origin(target['url'])
                   and tab['state'] not in {'closed', 'missing'} for tab in snapshot['tabs']):
            raise rerun.RerunError('Record this exact new form tab through the resource contract before connecting it.')
        bridge = self.profile_bridge(row)
        bridge.validate(profile['profile_proof'])
        binding = bridge.connect(row['job_id'], owner, profile['profile_proof'], target)
        if binding.get('owner_thread_id') != owner or binding.get('job_id') != row['job_id'] or binding.get('state') != 'connected':
            raise rerun.RerunError('The exact form jump is not connected. Stop before filling.')
        self.guard(row)
        details['paired_tab'] = binding | target
        with self.gate._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.same_observation(db, row):
                raise rerun.RerunError('The restart changed while pairing. Do not fill the form.')
            db.execute('UPDATE application_restarts SET details=? WHERE request_id=?', (json.dumps(details), row['request_id']))
        return {'status': 'connected', **binding}

    def prepare(self, row, owner):
        with rerun.worker_stopped(self.root):
            self.inspect(row, owner)
            if row['phase'] not in {'relay_pending', 'cleanup'}:
                return {'status': 'reconcile_only', 'phase': row['phase'], 'candidate_owner': row['candidate_owner']}
            details = json.loads(row['details'])
            self.profile_bridge(row).validate((details.get('profile') or {}).get('profile_proof'))
            self.closed_resources(row)
            with self.gate._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT state FROM application_reruns WHERE request_id=?', (row['request_id'],)).fetchone()
                if current['state'] not in rerun.OPEN_STATES or rerun.stop_snapshot(db, row['scope'], row['job_id']) != json.loads(row['stops']):
                    raise rerun.RerunError('A newer stop revoked replacement creation.')
                changed = db.execute("UPDATE application_restarts SET phase='create_unknown' WHERE request_id=? AND phase IN ('relay_pending','cleanup')", (row['request_id'],)).rowcount
            if not changed:
                return {'status': 'reconcile_only'}
            return {'status': 'official_create_required', 'tool': 'create_thread', 'arguments': details['create_arguments'], 'after_send': self.command(row['request_id'], 'ack') + ' --owner "$CODEX_THREAD_ID" --target RETURNED_THREAD_ID'}

    def closed_resources(self, row):
        details = json.loads(row['details'])
        snapshot = CoordinationStore(self.root, scope=row['scope'], jobfeed_db=self.service.jobfeed_db).snapshot([row['job_id']])
        current = {(tab['browser_id'], tab['tab_id']): tab for tab in snapshot['tabs']}
        for tab in details['tabs']:
            observed = current.get((tab['browser_id'], tab['tab_id']))
            if not observed or observed['state'] not in {'closed', 'missing'} or observed['observed_at'] < row['created_at']:
                raise rerun.RerunError('The previous exact form tab has not been verified closed or missing.')
        if any(tab['state'] not in {'closed', 'missing'} for tab in snapshot['tabs']) or any(claim['status'] != 'released' for claim in snapshot['claims']):
            raise rerun.RerunError('The old applier still owns an unresolved tab or resource.')

    def recover(self, row, operator, target):
        if not operator or not target or target == row['owner_thread_id']:
            raise rerun.RerunError('A real recovery operator and the existing replacement are required.')
        with rerun.worker_stopped(self.root):
            row = self.row(row['request_id'])
            self.guard(row)
            _, _, relay_state = self.relay_turn(row, active=False)
            if relay_state not in {'active', 'completed'}:
                raise rerun.RerunError('The original relay is not verified active or completed.')
            if row['phase'] not in {'relay_pending', 'cleanup', 'create_unknown', 'candidate_received', 'adopting', 'adopted', 'filling'}:
                raise rerun.RerunError('This restart cannot recover an existing creation.')
            if row['candidate_owner'] not in {None, target}:
                raise rerun.RerunError('The existing replacement candidate cannot change.')
            candidate = row | {'candidate_owner': target}
            thread, turn = self.target(candidate)
            details = json.loads(row['details'])
            if row['phase'] in {'adopted', 'filling'}:
                return {'status': 'adopted', 'thread_id': target, 'archive_previous_thread': row['owner_thread_id']}
            self.closed_resources(row)
            if rerun.host_turn_state(thread, turn['id'], details['project']) != 'active':
                raise rerun.RerunError('The existing replacement initial turn is no longer active.')
            details['recovery'] = {'operator': operator, 'target': target, 'turn_id': turn['id'], 'verified_at': utc_now()}
            with self.gate._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if not self.same_observation(db, row) or rerun.stop_snapshot(db, row['scope'], row['job_id']) != json.loads(row['stops']):
                    raise rerun.RerunError('The restart changed during recovery. Re-read the same request.')
                db.execute("UPDATE application_restarts SET phase='adopting',candidate_owner=?,target_turn=?,details=? WHERE request_id=?", (target, turn['id'], json.dumps(details), row['request_id']))
            self.adopt(self.row(row['request_id']), recovery=True)
            return {'status': 'adopted', 'thread_id': target, 'archive_previous_thread': row['owner_thread_id']}

    def acknowledge(self, row, owner, target):
        if owner != row['owner_thread_id'] or not target or target == owner:
            raise rerun.RerunError('The original relay and a replacement Desktop chat are required.')
        with rerun.worker_stopped(self.root):
            self.guard(row)
            self.relay_turn(row)
            if row['phase'] not in {'create_unknown', 'candidate_received', 'adopting', 'adopted', 'filling'}:
                raise rerun.RerunError('Replacement creation was not prepared.')
            if row['candidate_owner'] not in {None, target}:
                raise rerun.RerunError('The replacement candidate cannot change.')
            with self.gate._db() as db:
                db.execute("UPDATE application_restarts SET candidate_owner=?,phase=CASE WHEN phase='create_unknown' THEN 'candidate_received' ELSE phase END WHERE request_id=? AND (candidate_owner IS NULL OR candidate_owner=?)", (target, row['request_id'], target))
            row = self.row(row['request_id'])
            try:
                _, turn = self.target(row)
            except (OSError, ValueError, KeyError):
                return {'status': 'receipt_pending', 'thread_id': target, 'next': 'Retry acknowledgment only. Never create again.'}
            with self.gate._db() as db:
                db.execute("UPDATE application_restarts SET target_turn=?,phase=CASE WHEN phase='candidate_received' THEN 'adopting' ELSE phase END WHERE request_id=?", (turn['id'], row['request_id']))
            row = self.row(row['request_id'])
            self.adopt(row)
            return {'status': 'adopted', 'thread_id': target, 'archive_previous_thread': owner}

    def adopt(self, row, *, recovery=False):
        details = self.guard(row)
        thread, turn = self.target(row)
        if row['phase'] in {'adopted', 'filling'}:
            return
        if row['phase'] != 'adopting':
            raise rerun.RerunError('The replacement is not ready for adoption.')
        if rerun.host_turn_state(thread, turn['id'], details['project']) != 'active':
            raise rerun.RerunError('The replacement initial turn ended before adoption.')
        from pipeline.config import PipelineConfig
        from pipeline.resume_catalog import transfer_selection, publish, encoded
        config = PipelineConfig.discover(self.root)
        transfer_selection(config, row['scope'], row['job_id'], row['owner_thread_id'], row['candidate_owner'], row['request_id'])
        self.guard(row)
        folder = Path(details['folder'])
        task = rerun._object(folder / 'application-task.json')
        task['submit_authorized'] = False
        task['restart'] = {'request_id': row['request_id'], 'previous_owner': row['owner_thread_id'], 'owner': row['candidate_owner'], 'turn_id': row['target_turn'], 'profile': details['profile']}
        publish(folder / 'application-task.json', encoded(task))
        from pipeline.resource_cli import task_contract
        publish(folder / 'resource-contract.json', encoded(task_contract(self.root, row['scope'], row['job_id'], row['candidate_owner'])))
        resources = CoordinationStore(self.root, scope=row['scope'], jobfeed_db=self.service.jobfeed_db)
        _, _, relay_state = self.relay_turn(row, active=not recovery)
        if recovery and relay_state not in {'active', 'completed'}:
            raise rerun.RerunError('The original relay changed during recovery.')
        with self.gate._db() as db:
            db.execute('ATTACH DATABASE ? AS jobs', (str(self.service.jobfeed_db),))
            db.execute('ATTACH DATABASE ? AS resources', (str(resources.path),))
            db.execute('BEGIN IMMEDIATE')
            if rerun.stop_snapshot(db, row['scope'], row['job_id']) != json.loads(row['stops']) or db.execute('SELECT state FROM application_reruns WHERE request_id=?', (row['request_id'],)).fetchone()[0] not in rerun.OPEN_STATES:
                raise rerun.RerunError('A newer stop revoked adoption.')
            current = db.execute('SELECT codex_application_session_id FROM jobs.application_queue WHERE job_id=?', (row['job_id'],)).fetchone()
            binding = db.execute('SELECT owner FROM resources.coordination_bindings WHERE scope=? AND job_id=?', (row['scope'], row['job_id'])).fetchone()
            if not current or current[0] not in {row['owner_thread_id'], row['candidate_owner']} or not binding or binding[0] not in {row['owner_thread_id'], row['candidate_owner']}:
                raise rerun.RerunError('The application or resource owner changed during adoption.')
            if db.execute("SELECT 1 FROM resources.coordination_claims WHERE scope=? AND job_id=? AND status!='released'", (row['scope'], row['job_id'])).fetchone():
                raise rerun.RerunError('An unresolved shared resource prevents adoption.')
            prior = db.execute('SELECT scope,job_id FROM execution_bindings WHERE owner=?', (row['candidate_owner'],)).fetchone()
            if prior and tuple(prior) != (row['scope'], row['job_id']):
                raise rerun.RerunError('The replacement is already bound to another application.')
            db.execute('INSERT OR IGNORE INTO execution_bindings VALUES(?,?,?)', (row['candidate_owner'], row['scope'], row['job_id']))
            db.execute("UPDATE jobs.application_queue SET codex_application_session_id=?,state='Applying' WHERE job_id=?", (row['candidate_owner'], row['job_id']))
            db.execute('UPDATE resources.coordination_bindings SET owner=?,revision=revision+1 WHERE scope=? AND job_id=? AND owner=?', (row['candidate_owner'], row['scope'], row['job_id'], row['owner_thread_id']))
            db.execute("UPDATE application_restarts SET phase='adopted' WHERE request_id=?", (row['request_id'],))
            db.execute("UPDATE application_reruns SET state='running',reason=NULL WHERE request_id=?", (row['request_id'],))

    def await_adoption(self, row, owner):
        self.guard(row)
        if row['candidate_owner'] not in {None, owner}:
            raise rerun.RerunError('This is not the registered replacement chat.')
        if row['phase'] not in {'adopted', 'filling'}:
            return {'status': 'waiting_for_adoption'}
        thread, turn = self.target(row)
        if rerun.host_turn_state(thread, turn['id'], json.loads(row['details'])['project']) != 'active':
            raise rerun.RerunError('The replacement preparation turn is no longer active.')
        verify = shlex.join([str(self.root / '.venv/bin/python'), '-m', 'pipeline.resume_catalog', '--root', str(self.root), 'verify', '--scope', row['scope'], '--job', str(row['job_id']), '--owner', owner, '--restart-request', row['request_id'], '--turn-id', turn['id']])
        details = json.loads(row['details'])
        return {'status': 'adopted', 'owner': owner, 'turn_id': turn['id'], 'resume_verify_command': verify, 'stop_before_submit': True, 'verified_profile': details.get('profile'), 'pair_before_filling': True}

    def reconcile(self, row, *, strict=False):
        row = self.row(row['request_id'])
        if row['state'] not in rerun.OPEN_STATES:
            if row['state'] == 'blocked' and row['phase'] == 'adopting':
                self.rollback(row)
            return row
        try:
            details = self.guard(row)
            if row['phase'] in {'adopted', 'filling'}:
                thread, turn = self.target(row)
                state = rerun.host_turn_state(thread, turn['id'], details['project'])
                phase, public_state = {'active': ('filling', 'running'), 'completed': ('completed', 'completed'), 'interrupted': ('blocked', 'blocked')}[state]
                if state == 'completed' and not is_archived(row['owner_thread_id'], details['project']):
                    phase, public_state = row['phase'], 'running'
            else:
                _, _, state = self.relay_turn(row, active=False)
                phase = 'cleanup' if row['phase'] == 'relay_pending' else row['phase']
                public_state = 'running' if state == 'active' else 'unknown'
            self.observe(row, phase, public_state)
        except rerun.RerunError as exc:
            if strict:
                raise
            if exc.code != 'unavailable':
                changed = self.observe(row, row['phase'], 'blocked', str(exc))
                if changed and row['phase'] == 'adopting':
                    self.rollback(self.row(row['request_id']))
        except (OSError, ValueError, KeyError, sqlite3.Error):
            if strict:
                raise
        return self.row(row['request_id'])

    @staticmethod
    def same_observation(db, row):
        current = db.execute('SELECT r.state,s.phase,s.candidate_owner,s.target_turn FROM application_reruns r JOIN application_restarts s USING(request_id) WHERE request_id=?', (row['request_id'],)).fetchone()
        return current is not None and tuple(current) == tuple(row[key] for key in ('state', 'phase', 'candidate_owner', 'target_turn'))

    def observe(self, row, phase, state, reason=None):
        with self.gate._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.same_observation(db, row):
                return False
            db.execute('UPDATE application_restarts SET phase=? WHERE request_id=?', (phase, row['request_id']))
            db.execute('UPDATE application_reruns SET state=?,reason=? WHERE request_id=?', (state, reason, row['request_id']))
            return True

    def rollback(self, row):
        from pipeline.config import PipelineConfig
        from pipeline.resume_catalog import restore_selection, publish, encoded
        config = PipelineConfig.discover(self.root)
        if not authorize_resume_rollback(config, row['scope'], row['job_id'], row['owner_thread_id'], row['candidate_owner'], row['request_id']):
            return
        details = json.loads(row['details'])
        folder = Path(details['folder'])
        if not (folder / 'resume-restarts' / (row['request_id'] + '.json')).exists():
            return
        restore_selection(config, row['scope'], row['job_id'], row['owner_thread_id'], row['candidate_owner'], row['request_id'])
        task = rerun._object(folder / 'application-task.json')
        if task.get('restart', {}).get('request_id') == row['request_id']:
            publish(folder / 'application-task.json', encoded(details['previous_task'] | {'submit_authorized': False}))
        resources = rerun._object(folder / 'resource-contract.json')
        if resources.get('owner') == row['candidate_owner']:
            publish(folder / 'resource-contract.json', encoded(details['previous_resource_contract']))


def _service(config):
    return Restarts(rerun.ApplicationReruns(config.resume_root, config.jobfeed_db))


def authorize_resume_transfer(config, scope, job_id, previous_owner, owner, request_id):
    try:
        service = _service(config)
        row = service.row(request_id)
        service.guard(row)
        service.target(row)
        return (row['scope'], row['job_id'], row['owner_thread_id'], row['candidate_owner']) == (str(scope), job_id, previous_owner, owner) and row['phase'] == 'adopting'
    except Exception:
        return False


def authorize_resume_rollback(config, scope, job_id, previous_owner, owner, request_id):
    try:
        service = _service(config)
        row = service.row(request_id)
        item = rerun.ApplicationQueue(config.jobfeed_db).get(job_id)
        return (row['scope'], row['job_id'], row['owner_thread_id'], row['candidate_owner']) == (str(scope), job_id, previous_owner, owner) and row['phase'] == 'adopting' and row['state'] == 'blocked' and item.codex_application_session_id == previous_owner
    except Exception:
        return False


def permits_resume_verify(config, scope, job_id, owner, request_id, turn_id):
    try:
        service = _service(config)
        row = service.row(request_id)
        details = json.loads(row['details'])
        paired = details.get('paired_tab', {})
        if details.get('profile_guard_version') and (paired.get('owner_thread_id') != owner or paired.get('state') != 'connected'):
            return False
        return (row['scope'], row['job_id'], row['candidate_owner'], row['target_turn']) == (str(scope), job_id, owner, turn_id) and row['phase'] in {'adopted', 'filling'} and permits_turn(service.gate, owner, turn_id)
    except Exception:
        return False


def permits_turn(gate, owner, turn_id):
    if not turn_id:
        return False
    with gate._db() as db:
        saved = db.execute("SELECT r.request_id,r.jobfeed_db FROM application_reruns r JOIN application_restarts s USING(request_id) WHERE (r.owner_thread_id=? OR s.candidate_owner=?) AND r.state IN ('unknown','queued','running') ORDER BY r.created_at DESC LIMIT 1", (owner, owner)).fetchone()
    if not saved:
        return False
    service = Restarts(rerun.ApplicationReruns(gate.path.parent.parent, saved['jobfeed_db']))
    try:
        row = service.row(saved['request_id'])
        if owner == row['owner_thread_id']:
            _, turn, state = service.relay_turn(row)
            return turn['id'] == turn_id and state == 'active'
        if row['phase'] not in {'adopted', 'filling'}:
            return False
        thread, turn = service.target(row)
        return turn['id'] == turn_id and rerun.host_turn_state(thread, turn_id, json.loads(row['details'])['project']) == 'active'
    except Exception:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--jobfeed-db', type=Path, required=True)
    parser.add_argument('command', choices=['inspect', 'prepare', 'ack', 'await-adoption', 'recover', 'verify-profile', 'pair-tab'])
    parser.add_argument('request_id')
    parser.add_argument('--owner')
    parser.add_argument('--operator')
    parser.add_argument('--target')
    args = parser.parse_args(argv)
    try:
        identity = args.operator if args.command == 'recover' else args.owner
        if not identity or os.environ.get('CODEX_THREAD_ID') != identity:
            raise rerun.RerunError('The command must run in its actual Desktop chat.')
        service = Restarts(rerun.ApplicationReruns(args.root, args.jobfeed_db))
        row = service.row(args.request_id)
        if args.command == 'recover':
            result = service.recover(row, args.operator, args.target)
        elif args.command in {'verify-profile', 'pair-tab'}:
            payload = json.load(sys.stdin)
            result = getattr(service, args.command.replace('-', '_'))(row, args.owner, payload)
        elif args.command == 'ack':
            result = service.acknowledge(row, args.owner, args.target)
        else:
            result = getattr(service, args.command.replace('-', '_'))(row, args.owner)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
