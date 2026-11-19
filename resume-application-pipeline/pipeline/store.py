"""Persistent application queue stored alongside Jobfeed data."""

from __future__ import annotations

import sqlite3
import re
import json
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from pipeline.results import ADVICE_STATUSES, validate_advice_request, validate_advice_result
from pipeline.lifecycle import ApplicationState, TERMINAL_STATES, validate_transition
from pipeline.queue_admission import QueueAdmission, admit_job


HOLD_KINDS = frozenset({'captcha'})
# A row whose hold is absent or cleared may be claimed.
UNHELD = "(hold IS NULL OR json_extract(hold,'$.cleared_at') IS NOT NULL)"


@dataclass(frozen=True)
class Hold:
    """Why a job waits for the user outside every dispatch path, kept in its queue row until the user clears it.

    id is the Applier report that raised it. A cleared hold stays as the record of who cleared it, until the next one."""
    id: str
    kind: str
    detail: str
    page_url: str | None
    created_at: str
    cleared_at: str | None = None
    cleared_by: str | None = None

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    @classmethod
    def parse(cls, text: str | None) -> Hold | None:
        return None if not text else cls(**json.loads(text))


@dataclass(frozen=True)
class QueueItem:
    job_id: int
    state: ApplicationState
    retry_count: int = 0
    manual_priority: bool = False
    next_retry_at: str | None = None
    application_folder_relpath: str | None = None
    codex_application_session_id: str | None = None
    claude_session_id: str | None = None
    claude_session_url: str | None = None
    hold: Hold | None = None

    @property
    def held(self) -> bool:
        return self.hold is not None and self.hold.active


class ApplicationQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS application_queue (
                       job_id INTEGER NOT NULL UNIQUE,
                       state TEXT NOT NULL,
                       retry_count INTEGER NOT NULL DEFAULT 0,
                       next_retry_at TEXT,
                       application_folder_relpath TEXT,
                       codex_application_session_id TEXT,
                       claude_session_id TEXT,
                       claude_session_url TEXT,
                       FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                   )"""
            )
            # Keep non-submission terminal states in the existing queue schema.
            # Jobfeed validates its schema strictly; do not add private tables.
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_terminal'").fetchone():
                connection.execute("""INSERT OR IGNORE INTO application_queue
                    (job_id,state,application_folder_relpath,codex_application_session_id)
                    SELECT job_id,state,application_folder_relpath,codex_application_session_id
                    FROM application_terminal WHERE state IN ('Cancelled','Skipped','Unavailable')""")
                connection.execute("DROP TABLE application_terminal")
            self._initialize_advice(connection)
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(application_queue)")
            }
            for name in (
                "claude_session_id",
                "claude_session_url",
                "submit_authorization",
                "hold",
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE application_queue ADD COLUMN {name} TEXT")
            if "manual_priority" not in columns:
                connection.execute("ALTER TABLE application_queue ADD COLUMN manual_priority INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_application_queue_state "
                "ON application_queue(state, next_retry_at)"
            )

    def record_submit_authorization(self, job_id: int, source: dict) -> dict:
        # Called by the control session receiving a real user message, never by Luna.
        if not isinstance(source,dict) or type(source.get('authorized')) is not bool:
            raise ValueError('authorized must be boolean')
        for key in ('user_text','source_thread_id','source_message_id'):
            if not isinstance(source.get(key),str) or not source[key].strip():
                raise ValueError('missing user authorization provenance: '+key)
        with closing(self._connect()) as connection, connection:
            item=connection.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id=?',(job_id,)).fetchone()
            if not item or not item[0]:
                raise ValueError('authorization requires an existing original Luna session')
            record={key:source[key] for key in ('authorized','user_text','source_thread_id','source_message_id')}
            record.update(job_id=job_id,luna_session_id=item[0],recorded_at=datetime.now().isoformat())
            connection.execute('UPDATE application_queue SET submit_authorization=? WHERE job_id=?',(json.dumps(record,ensure_ascii=False),job_id))
        return record

    def record_dashboard_submit_authorization(self, job_id: int, source: dict, *, dashboard=False) -> dict:
        # The user confirms in the local Dashboard, or by replying "submit" to the review summary in Discord, or turned on auto submit mode.
        confirmed_by = {'dashboard': source.get('actor') == 'local-user',
                        'discord': str(source.get('actor', '')).startswith('discord:') and bool(source.get('message_id')),
                        'auto': source.get('actor') == 'auto-submit' and bool(source.get('enabled_at')) and bool(source.get('enabled_by'))}
        if not dashboard or not confirmed_by.get(source.get('kind')):
            raise ValueError('explicit local dashboard confirmation required')
        if not all(isinstance(source.get(k), str) and source[k].strip() for k in ('event_id', 'confirmed_at', 'controller_thread_id')):
            raise ValueError('dashboard event provenance required')
        with closing(self._connect()) as connection, connection:
            item = connection.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
            if not item or item[0] != source.get('owner'):
                raise ValueError('dashboard submission owner changed')
            record = {'authorized': True, 'job_id': job_id, 'luna_session_id': item[0],
                      'source': dict(source), 'recorded_at': datetime.now().isoformat()}
            connection.execute('UPDATE application_queue SET submit_authorization=? WHERE job_id=?', (json.dumps(record), job_id))
        return record

    def submit_authorization(self, job_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row=connection.execute('SELECT submit_authorization,codex_application_session_id FROM application_queue WHERE job_id=?',(job_id,)).fetchone()
        if not row or not row[0]: return None
        record=json.loads(row[0])
        return record if record['luna_session_id']==row[1] else None

    def _initialize_advice(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS application_advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
            luna_session_id TEXT NOT NULL, revision INTEGER NOT NULL,
            request TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
            advisor_thread_id TEXT, advisor_turn_id TEXT, delivery_turn_id TEXT,
            notification_turn_id TEXT, notification_error TEXT, pause_scope TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        columns = {row[1] for row in connection.execute('PRAGMA table_info(application_advice)')}
        for name, kind in [('controller_error', 'TEXT'), ('delivery_prepared_at', 'TEXT'), ('delivery_owner_required', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                connection.execute(f'ALTER TABLE application_advice ADD COLUMN {name} {kind}')
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_advice_open_job
            ON application_advice(job_id) WHERE status <> 'resolved'""")

    def request_advice(self, job_id: int, payload: dict, *, resume_critical: bool = False, supersede_pending: bool = False) -> dict:
        request = validate_advice_request(payload)
        # Missing candidate facts and research questions wait for advice; they are not
        # system-wide correctness failures merely because Luna set a suspicion flag.
        risk_evidence = ' '.join(request.get('evidence', []))
        serious = request.get('blocker_kind') == 'submission_unknown' or re.search(
            r'wrong resume|incorrect resume|resume mismatch|multiple applications|cross[- ](?:job|task)|submission (?:unknown|uncertain)|unknown submission|错误简历|简历.*不匹配|跨岗位|跨任务|提交结果.*(?:不明|未知)',
            risk_evidence, re.I)
        if request['category'] in {'missing_fact', 'application_answer'} and request['suspected_critical'] and not serious:
            request = {**request, 'suspected_critical': False}
            request.pop('pause_scope', None)
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            job = connection.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
            if job is None or job[0] != request['luna_session_id']:
                raise ValueError('advice job/session does not match current application')
            now = datetime.now().isoformat()
            request["requested_at"] = now
            status = 'critical' if request['suspected_critical'] else 'pending'
            scope = request['pause_scope'] if request['suspected_critical'] else None
            advice_id = request.get('advice_id')
            if advice_id is None:
                if request['revision'] != 1:
                    raise ValueError('new advice revision must be 1')
                if connection.execute("SELECT id FROM application_advice WHERE job_id=? AND status<>'resolved'", (job_id,)).fetchone():
                    raise ValueError('application already has an unresolved question')
                cursor = connection.execute('INSERT INTO application_advice(job_id,luna_session_id,revision,request,status,pause_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (job_id, request['luna_session_id'], 1, json.dumps(request), status, scope, now, now))
                advice_id = cursor.lastrowid
            else:
                current = connection.execute('SELECT * FROM application_advice WHERE id=?', (advice_id,)).fetchone()
                if current is None or current['job_id'] != job_id or current['luna_session_id'] != request['luna_session_id']:
                    raise ValueError('followup does not match advice/application')
                if current['status'] == 'resolved':
                    raise ValueError(f'advice {advice_id} is resolved; for a new obstacle omit advice_id and use revision 1; do not increment the resolved request')
                allowed_phases = ('delivering', 'needs_user', 'critical') if resume_critical else ('delivering', 'needs_user')
                if supersede_pending:
                    allowed_phases += ('pending',)
                if current['status'] not in allowed_phases or request['revision'] != current['revision'] + 1:
                    raise ValueError('followup requires next revision of delivering/needs_user advice')
                previous=json.loads(current['request'])
                previous_result=json.loads(current['result']) if current['result'] else {}
                request['confirmation_corrected']=bool(previous.get('confirmation_corrected') or previous_result.get('confirmation_correction'))
                connection.execute('UPDATE application_advice SET revision=?,request=?,status=?,pause_scope=?,result=NULL,advisor_turn_id=NULL,delivery_turn_id=NULL,notification_turn_id=NULL,notification_error=NULL,delivery_prepared_at=NULL,delivery_owner_required=0,controller_error=NULL,updated_at=? WHERE id=?', (request['revision'], json.dumps(request), status, scope, now, advice_id))
            connection.commit()
            return self.get_advice(advice_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_advice(self, job_id: int, advice_id: int, revision: int, luna_session_id: str) -> dict:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            job = connection.execute('SELECT codex_application_session_id FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
            if job is None or job[0] != luna_session_id:
                raise ValueError('resolution job/session does not match current application')
            cursor = connection.execute("UPDATE application_advice SET status='resolved',updated_at=? WHERE id=? AND revision=? AND job_id=? AND luna_session_id=? AND status='delivering'", (datetime.now().isoformat(), advice_id, revision, job_id, luna_session_id))
            if cursor.rowcount != 1:
                raise ValueError('resolution requires matching current delivery and identity')
            connection.commit()
            return self.get_advice(advice_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_advice(self, advice_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM application_advice WHERE id=?', (advice_id,)).fetchone()
        return _advice_record(row) if row else None

    def current_advice(self, job_id: int) -> dict | None:
        rows = self.list_advice(job_ids=[job_id])
        return next((row for row in rows if row['status'] != 'resolved'), None)

    def list_advice(self, statuses=None, job_ids=None) -> list[dict]:
        conditions, values = [], []
        for column, selected in (('status', statuses), ('job_id', job_ids)):
            if selected is not None:
                selected = list(selected)
                if not selected:
                    return []
                conditions.append(column + ' IN (' + ','.join('?' for _ in selected) + ')')
                values.extend(selected)
        sql = 'SELECT * FROM application_advice'
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql + ' ORDER BY id', values).fetchall()
        return [_advice_record(row) for row in rows]

    def claim_advice(self, advice_id: int) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("UPDATE application_advice SET status='running',updated_at=? WHERE id=? AND status IN ('pending','controller_required')", (datetime.now().isoformat(), advice_id))
            return cursor.rowcount == 1

    def update_advice(self, advice_id: int, revision: int, **fields) -> dict:
        allowed = {'controller_error', 'delivery_prepared_at', 'delivery_owner_required', 'status', 'result', 'advisor_thread_id', 'advisor_turn_id', 'delivery_turn_id', 'notification_turn_id', 'notification_error', 'pause_scope'}
        if not fields or set(fields) - allowed:
            raise ValueError('unsupported advice update')
        if 'status' in fields and (not isinstance(fields['status'], str) or fields['status'] not in ADVICE_STATUSES):
            raise ValueError('unknown advice status')
        if 'pause_scope' in fields and fields['pause_scope'] not in (None, 'job', 'global'):
            raise ValueError('invalid pause scope')
        if fields.get('result') is not None:
            result = validate_advice_result(fields['result'])
            if result['decision'] == 'critical':
                fields['status'] = 'critical'
                fields['pause_scope'] = result['pause_scope']
            fields['result'] = json.dumps(result)
        fields['updated_at'] = datetime.now().isoformat()
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM application_advice WHERE id=? AND revision=?', (advice_id, revision)).fetchone()
            if current is None:
                raise ValueError('missing advice or stale revision')
            status = fields.get('status', current['status'])
            scope = fields.get('pause_scope', current['pause_scope'])
            if status == 'critical' and scope not in ('job', 'global'):
                raise ValueError('critical status requires pause scope')
            connection.execute('UPDATE application_advice SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=? AND revision=?', [*fields.values(), advice_id, revision])
            connection.commit()
            return self.get_advice(advice_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def paused(self, job_id: int | None = None) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT 1 FROM application_advice WHERE status='critical' AND (pause_scope='global' OR (pause_scope='job' AND job_id=?)) LIMIT 1", (job_id,)).fetchone()
        return row is not None

    def hold(self, job_id: int, hold: Hold) -> bool:
        """Hold the job unless it is already held or already carried this hold. Returns whether it was set."""
        if hold.kind not in HOLD_KINDS:
            raise ValueError(f"unknown hold kind {hold.kind!r}")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT hold FROM application_queue WHERE job_id=?", (job_id,)).fetchone()
            current = Hold.parse(row["hold"]) if row else None
            if row is None or (current is not None and (current.active or current.id == hold.id)):
                return False
            connection.execute("UPDATE application_queue SET hold=? WHERE job_id=?", (json.dumps(asdict(hold), ensure_ascii=False), job_id))
            return True

    def clear_hold(self, job_id: int, cleared_by: str, hold_id: str | None = None) -> Hold | None:
        """Clear the job's active hold, or only the one with hold_id. Returns the cleared hold, or None when none matched."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT hold FROM application_queue WHERE job_id=?", (job_id,)).fetchone()
            current = Hold.parse(row["hold"]) if row else None
            if current is None or not current.active or hold_id not in (None, current.id):
                return None
            cleared = replace(current, cleared_at=datetime.now(timezone.utc).isoformat(), cleared_by=cleared_by)
            connection.execute("UPDATE application_queue SET hold=? WHERE job_id=?", (json.dumps(asdict(cleared), ensure_ascii=False), job_id))
            return cleared

    def holds(self) -> list[tuple[int, str | None, Hold]]:
        """(job_id, owner, hold) for every job carrying a hold, active or cleared."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT job_id,codex_application_session_id,hold FROM application_queue WHERE hold IS NOT NULL ORDER BY rowid"
            ).fetchall()
        return [(int(row["job_id"]), row["codex_application_session_id"], Hold.parse(row["hold"])) for row in rows]

    def enqueue(self, job_id: int) -> bool:
        return self.admit(job_id).status == "queued"

    def admit(self, job_id: int, *, manual_priority: bool = False) -> QueueAdmission:
        with closing(self._connect()) as connection:
            return admit_job(connection, job_id, manual_priority=manual_priority)

    def get(self, job_id: int) -> QueueItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM application_queue WHERE job_id=?", (job_id,)
            ).fetchone()
        return _queue_item(row) if row is not None else None

    def list_active(self) -> list[QueueItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM application_queue WHERE state NOT IN ("
                + ",".join("?" for _ in TERMINAL_STATES) + ") ORDER BY manual_priority DESC, rowid",
                tuple(state.value for state in TERMINAL_STATES),
            ).fetchall()
        return [_queue_item(row) for row in rows]

    def list_all(self) -> list[QueueItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM application_queue ORDER BY manual_priority DESC, rowid"
            ).fetchall()
        return [_queue_item(row) for row in rows]

    def claim_queued(self, ordered_ids: list[int] | None = None) -> QueueItem | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = None
            if ordered_ids is None:
                row = connection.execute(
                    f"SELECT job_id FROM application_queue WHERE state=? AND {UNHELD} ORDER BY manual_priority DESC, rowid LIMIT 1",
                    (ApplicationState.QUEUED.value,),
                ).fetchone()
            else:
                priorities = {int(item["job_id"]): bool(item["manual_priority"]) for item in connection.execute(
                    "SELECT job_id,manual_priority FROM application_queue WHERE state=?", (ApplicationState.QUEUED.value,)
                )}
                for candidate_id in sorted(ordered_ids, key=lambda candidate: not priorities.get(candidate, False)):
                    row = connection.execute(
                        f"SELECT job_id FROM application_queue WHERE state=? AND job_id=? AND {UNHELD}",
                        (ApplicationState.QUEUED.value, candidate_id),
                    ).fetchone()
                    if row is not None:
                        break
            if row is None:
                connection.rollback()
                return None
            job_id = int(row["job_id"])
            connection.execute(
                "UPDATE application_queue SET state=? WHERE job_id=? AND state=?",
                (
                    ApplicationState.DISPATCHING.value,
                    job_id,
                    ApplicationState.QUEUED.value,
                ),
            )
            connection.commit()
            return self.get(job_id)
        finally:
            connection.close()

    def return_to_queue(self, job_id: int, previous: ApplicationState, owner: str | None, submit_authorization: str | None = None) -> bool:
        """Put a stranded Resume Ready or Applying job back in the queue as if newly admitted, keeping its priority.

        Only the user's requeue command calls this, after it retired the job's Applier. Lifecycle transitions never
        allow it. A submit authorization is cleared only when it is the one given, which the command judged spent on an
        attempt that sent nothing. Returns False, changing nothing, if the row is no longer in that state under that
        owner with that authorization."""
        if previous not in {ApplicationState.RESUME_READY, ApplicationState.APPLYING}:
            raise ValueError(f"only Resume Ready or Applying jobs go back to the queue, not {previous}")
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                """UPDATE application_queue SET state=?,retry_count=0,next_retry_at=NULL,application_folder_relpath=NULL,
                       codex_application_session_id=NULL,claude_session_id=NULL,claude_session_url=NULL,submit_authorization=NULL
                   WHERE job_id=? AND state=? AND codex_application_session_id IS ? AND submit_authorization IS ?""",
                (ApplicationState.QUEUED.value, job_id, previous.value, owner, submit_authorization),
            ).rowcount == 1

    def due_retries(self, now: datetime) -> list[QueueItem]:
        timestamp = now.isoformat()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM application_queue
                   WHERE state=? AND next_retry_at<=? ORDER BY manual_priority DESC,next_retry_at,rowid""",
                (ApplicationState.RETRY_LATER.value, timestamp),
            ).fetchall()
        return [_queue_item(row) for row in rows]

    def transition(
        self,
        job_id: int,
        target: ApplicationState,
        *,
        retry_count: int | None = None,
        next_retry_at: str | None = None,
        application_folder_relpath: str | None = None,
        codex_application_session_id: str | None = None,
        claude_session_id: str | None = None,
        claude_session_url: str | None = None,
    ) -> QueueItem:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM application_queue WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"job {job_id} is not in the application queue")
            current = _queue_item(row)
            validate_transition(current.state, target)
            updated = QueueItem(
                job_id=job_id,
                state=target,
                manual_priority=current.manual_priority,
                hold=current.hold,
                retry_count=current.retry_count if retry_count is None else retry_count,
                next_retry_at=next_retry_at,
                application_folder_relpath=(
                    application_folder_relpath
                    if application_folder_relpath is not None
                    else current.application_folder_relpath
                ),
                codex_application_session_id=(
                    codex_application_session_id
                    if codex_application_session_id is not None
                    else current.codex_application_session_id
                ),
                claude_session_id=(
                    claude_session_id
                    if claude_session_id is not None
                    else current.claude_session_id
                ),
                claude_session_url=(
                    claude_session_url
                    if claude_session_url is not None
                    else current.claude_session_url
                ),
            )
            if target in {ApplicationState.SUBMITTED, ApplicationState.ALREADY_APPLIED}:
                connection.execute("DELETE FROM application_queue WHERE job_id=?", (job_id,))
            else:
                connection.execute(
                    """UPDATE application_queue
                       SET state=?,retry_count=?,next_retry_at=?,
                           application_folder_relpath=?,codex_application_session_id=?,
                           claude_session_id=?,claude_session_url=?
                       WHERE job_id=?""",
                    (
                        updated.state.value,
                        updated.retry_count,
                        updated.next_retry_at,
                        updated.application_folder_relpath,
                        updated.codex_application_session_id,
                        updated.claude_session_id,
                        updated.claude_session_url,
                        job_id,
                    ),
                )
            connection.commit()
            return updated
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _queue_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        job_id=int(row["job_id"]),
        state=ApplicationState(str(row["state"])),
        manual_priority=bool(row["manual_priority"]),
        retry_count=int(row["retry_count"]),
        next_retry_at=row["next_retry_at"],
        application_folder_relpath=row["application_folder_relpath"],
        codex_application_session_id=row["codex_application_session_id"],
        claude_session_id=row["claude_session_id"],
        claude_session_url=row["claude_session_url"],
        hold=Hold.parse(row["hold"]) if "hold" in row.keys() else None,
    )


def _advice_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record['request'] = json.loads(record['request'])
    record['result'] = json.loads(record['result']) if record['result'] is not None else None
    return record
