"""阻碍台账: the only place blockers are judged, deduplicated and queued as 待办.

Facts come from progress_monitor job dicts. The program alone writes here via
observe(); the controller claims and completes 待办; the user is only told about
待用户处理 records. Nothing here touches business state or the browser.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import shlex

USER_KINDS = {'captcha', 'binding_terms', 'login_verification'}
AGENT_STOPS = {'needs_input', 'needs_verification', 'needs_advice', 'retryable_error', 'needs_strong_model'}
# Local warnings are already covered by hung / closeout records.
LOCAL_WARNINGS = {'close_evidence_missing', 'no_recent_tool_activity_not_proof_of_stop'}
# Kinds judged from the original agent's final result cannot be judged while its turn runs.
HOLD_WHILE_ACTIVE = USER_KINDS | AGENT_STOPS | {'missing_fact', 'ended_without_receipt'}
GRACE_SECONDS = 15 * 60   # the worker syncs and closes out first
HUNG_SECONDS = 20 * 60
WAIT_SECONDS = 3 * 60     # result wait after the controller marks a 待办 done
WAKE_SECONDS = 10 * 60
ESCALATE_AT = 3

LABELS = {'needs_strong_model': '需要高级模型', 'sync': '已提交未同步', 'closeout': '已同步未关页', 'terminal_closeout': '岗位已终止未关页',
          'submission_unknown': '提交结果待确认', 'missing_fact': '缺个人事实', 'needs_input': '需要补充信息',
          'needs_verification': '等待验证', 'needs_advice': '需要主控判断', 'retryable_error': '暂时出错',
          'ended_without_receipt': '回合结束无结果无回执', 'hung': '回合无动作', 'source_error': '来源或证据错误',
          'system': '系统错误', 'captcha': '验证码', 'binding_terms': '约束性条款', 'login_verification': '密码或登录'}
INSTRUCTIONS = {
    'needs_strong_model': '核对本岗位模型升级是否已派发；未处理则让原会话换高级模型继续',
    'sync': '程序 15 分钟内没完成正式同步；主控核对回执后执行 finish-submission，禁止重投',
    'closeout': '程序 15 分钟内没完成关页；主控让原 Agent 核验专属页关闭，禁止重投',
    'terminal_closeout': '岗位已终止；主控让原 Agent 核验专属页关闭，禁止重投',
    'submission_unknown': '主控核查提交结果，只核查不再点击，禁止重投',
    'missing_fact': '主控先查 profile / 答案库；确实没有才用「完成 --需要用户」转给用户',
    'ended_without_receipt': '主控核对原 Agent 最后一轮实际结果和回执，再决定续跑或收尾',
    'hung': '主控核对原 Agent 是否真的卡死，再决定中断或续跑',
    'source_error': '主控核对错误来源和证据文件，修正后等程序复核',
    'system': '主控核查系统来源错误（数据库、session 索引）',
}
DEFAULT_INSTRUCTION = '主控核对原 Agent 当前停点并给出下一步，不得直接转问用户'
ESCALATION = '换处理方式：升级 Sol 或主控直接接手，不要重复同一条指令，不要转给用户'
HANDOFF_RULE = '人工候选须先由主控核对当前证据、已有授权及资料；只有审核确认必须用户处理的结构化请求才通知用户。'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS issues(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, kind TEXT NOT NULL, handler TEXT NOT NULL,
    status TEXT NOT NULL, company TEXT, title TEXT, owner TEXT, evidence TEXT,
    first_seen_at TEXT, episode_at TEXT, last_seen_at TEXT, closed_at TEXT,
    done_at TEXT, wait_from TEXT, notified_at TEXT, episodes INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS todos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id INTEGER NOT NULL, status TEXT NOT NULL,
    attempt INTEGER NOT NULL, instruction TEXT, created_at TEXT, claimed_at TEXT, done_at TEXT,
    summary TEXT, needs_user INTEGER NOT NULL DEFAULT 0);
'''


def _dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _seconds(now, then):
    return (_dt(now) - _dt(then)).total_seconds()


def _latest(*stamps):
    stamps = [s for s in stamps if s]
    return max(stamps, key=_dt) if stamps else None


def _stop_text(job):
    reason, quote = job.get('result_reason'), job.get('result_quote')
    text = '；'.join(x for x in (reason, f'原文：{quote}' if quote else None) if x)
    return text or f"停点状态 {job.get('blocked')}"


def classify(job, now):
    """The one table deciding 阻碍类别 → 处理方; returns (kind, handler, evidence) or None."""
    from pipeline.tab_preservation import application_finished
    if application_finished(job) or job.get('closed') or job.get('paused'):
        return None
    submitted = job.get('submitted')
    if job.get('user_submitted') and not submitted:
        return 'submission_unknown', 'controller', '用户报告已提交；保存确认截图或邮件后才能核验，禁止重投'
    if submitted and not job.get('synced'):
        return 'sync', 'program', '确认凭据已保存但正式同步未读回'
    blocked = job.get('blocked')
    if blocked == 'submission_unknown':
        return blocked, 'controller', _stop_text(job)
    if blocked == 'needs_strong_model':
        return blocked, 'program', _stop_text(job)
    if blocked in AGENT_STOPS:
        kind = job.get('blocker_kind')
        if kind in USER_KINDS:
            return kind, 'controller', _stop_text(job)
        return (kind if kind == 'missing_fact' else blocked), 'controller', _stop_text(job)
    state = job.get('turn_state')
    if state in {'ended', 'interrupted'} and job.get('owner') and not job.get('result_status'):
        return 'ended_without_receipt', 'controller', '原 Agent 本轮结束，没有可解析结果也没有回执'
    if state == 'active':
        activity = _latest(job.get('last_tool_at'), job.get('turn_at'))
        if activity and _seconds(now, activity) >= HUNG_SECONDS:
            return 'hung', 'controller', f'回合 active，自 {activity} 起没有任何动作'
    errors = [e for e in job.get('errors') or [] if e not in LOCAL_WARNINGS]
    if errors:
        return 'source_error', 'controller', '；'.join(errors)
    return None


def _settled(job):
    return bool(job.get('submitted') or job.get('user_submitted') or job.get('closed')
                or job.get('terminal') or job.get('paused'))


class BlockerLedger:
    def __init__(self, path):
        self.path = path
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        self.db.execute('COMMIT')

    # ---- the four operations ------------------------------------------------
    def observe(self, jobs, errors, now):
        """Judge every job once; returns 问题记录 that newly became 待用户处理."""
        now = _dt(now).isoformat()
        found = {}
        for job in jobs:
            verdict = classify(job, now)
            if verdict:
                found[(job['job_id'], verdict[0])] = (job, *verdict)
        if errors:
            found[(None, 'system')] = ({}, 'system', 'controller', '；'.join(sorted(set(errors))))
        by_job = {j['job_id']: j for j in jobs}
        with self._tx():
            for (job_id, kind), (job, _, handler, evidence) in found.items():
                self._seen(job_id, kind, handler, evidence, job, now)
            for issue in self.db.execute("SELECT * FROM issues WHERE status != 'closed'"):
                if (issue['job_id'], issue['kind']) in found:
                    continue
                job = by_job.get(issue['job_id'])
                # An unreadable source cannot prove a blocker gone; a running turn
                # cannot be judged for kinds read from the agent's final result.
                if issue['job_id'] is not None and errors:
                    continue
                if (issue['kind'] in HOLD_WHILE_ACTIVE and job and job.get('turn_state') == 'active'
                        and not _settled(job)):
                    continue
                self._close(issue['id'], now)
            fresh = self.db.execute("SELECT * FROM issues WHERE status='parked' AND notified_at IS NULL ORDER BY id").fetchall()
            self.db.execute("UPDATE issues SET notified_at=? WHERE status='parked' AND notified_at IS NULL", (now,))
        return [self._issue(dict(row, notified_at=now)) for row in fresh]

    def next(self, now):
        """Hand the controller the oldest unclaimed 待办, or None when the queue is empty."""
        now = _dt(now).isoformat()
        with self._tx():
            row = self.db.execute("SELECT * FROM todos WHERE status='unclaimed' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE todos SET status='claimed', claimed_at=? WHERE id=?", (now, row['id']))
        return self._todo(self.db.execute('SELECT * FROM todos WHERE id=?', (row['id'],)).fetchone())

    def done(self, todo_id, summary, now, needs_user=False):
        """Controller finished a 待办: the record waits for the result, or goes to the user."""
        now = _dt(now).isoformat()
        with self._tx():
            row = self.db.execute("SELECT * FROM todos WHERE id=? AND status!='done'", (todo_id,)).fetchone()
            if not row:
                raise ValueError(f'待办 {todo_id} 不存在或已完成')
            self.db.execute("UPDATE todos SET status='done', done_at=?, summary=?, needs_user=? WHERE id=?",
                            (now, summary, int(bool(needs_user)), todo_id))
            issue = self.db.execute('SELECT * FROM issues WHERE id=?', (row['issue_id'],)).fetchone()
            if row['status'] != 'cancelled' and issue['status'] != 'closed':
                if needs_user:
                    self.db.execute("UPDATE issues SET status='parked', handler='user', notified_at=NULL, done_at=?, wait_from=NULL WHERE id=?",
                                    (now, issue['id']))
                else:
                    self.db.execute("UPDATE issues SET status='waiting', done_at=?, wait_from=NULL WHERE id=?", (now, issue['id']))
        return self._issue(self.db.execute('SELECT * FROM issues WHERE id=?', (row['issue_id'],)).fetchone())

    def parked(self):
        return [self._issue(r) for r in self.db.execute("SELECT * FROM issues WHERE status='parked' ORDER BY id")]

    # ---- read-only helpers for the board and the heartbeat ---------------------
    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM todos WHERE status='unclaimed'").fetchone()[0]

    def wake_needed(self, last_wake_at, now):
        """Wake when the queue just filled, or the controller claimed nothing for 10 minutes."""
        if not self.pending_count():
            return False
        if not last_wake_at:
            return True
        claimed = self.db.execute("SELECT max(claimed_at) FROM todos WHERE claimed_at IS NOT NULL").fetchone()[0]
        return _seconds(now, _latest(last_wake_at, claimed)) >= WAKE_SECONDS

    def board(self):
        queue = [self._todo(r) for r in self.db.execute(
            "SELECT * FROM todos WHERE status IN ('unclaimed','claimed') ORDER BY id")]
        waiting = [self._issue(r) for r in self.db.execute("SELECT * FROM issues WHERE status='waiting' ORDER BY id")]
        return dict(parked=self.parked(), queue=queue, waiting=waiting)

    # ---- internals --------------------------------------------------------------
    def _seen(self, job_id, kind, handler, evidence, job, now):
        row = self.db.execute('SELECT * FROM issues WHERE job_id IS ? AND kind=?', (job_id, kind)).fetchone()
        fields = dict(company=job.get('company') if job else '系统', title=job.get('title') if job else None,
                      owner=job.get('owner') if job else None, evidence=evidence, last_seen_at=now)
        if not row:
            status = 'parked' if handler == 'user' else 'open'
            self.db.execute('INSERT INTO issues(job_id, kind, handler, status, company, title, owner, evidence, first_seen_at, episode_at, last_seen_at) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (job_id, kind, handler, status, fields['company'], fields['title'], fields['owner'], evidence, now, now, now))
            issue = self.db.execute('SELECT * FROM issues WHERE job_id IS ? AND kind=?', (job_id, kind)).fetchone()
            if handler == 'controller':
                self._enqueue(issue, now)
            return
        if row['status'] == 'closed':
            # Same blocker again: reopen the record with its history, as a new episode.
            self.db.execute('UPDATE issues SET status=?, handler=?, episode_at=?, closed_at=NULL, done_at=NULL, wait_from=NULL, '
                            'notified_at=NULL, episodes=episodes+1 WHERE id=?',
                            ('parked' if handler == 'user' else 'open', handler, now, row['id']))
            if handler == 'controller':
                self._enqueue(row, now)
        self.db.execute('UPDATE issues SET company=?, title=?, owner=?, evidence=?, last_seen_at=? WHERE id=?',
                        (*fields.values(), row['id']))
        row = self.db.execute('SELECT * FROM issues WHERE id=?', (row['id'],)).fetchone()
        if row['status'] == 'open' and row['handler'] == 'program':
            queued = self.db.execute("SELECT 1 FROM todos WHERE issue_id=? AND status!='cancelled' AND created_at>=?",
                                     (row['id'], row['episode_at'])).fetchone()
            if not queued and _seconds(now, row['episode_at']) >= GRACE_SECONDS:
                self._enqueue(row, now)
        elif row['status'] == 'waiting':
            self._wait(row, job, now)

    def _wait(self, issue, job, now):
        active = job.get('turn_state') == 'active' if job else False
        activity = _latest(job.get('last_tool_at'), job.get('turn_at')) if job else None
        if active and activity and _seconds(now, activity) < WAIT_SECONDS:
            self.db.execute('UPDATE issues SET wait_from=NULL WHERE id=?', (issue['id'],))
            return
        start = issue['wait_from']
        if not start:
            # Count from the turn's end, or from three quiet minutes inside a running turn.
            if active:
                start = (_dt(activity).timestamp() + WAIT_SECONDS) if activity else _dt(now).timestamp()
                start = datetime.fromtimestamp(start, timezone.utc).isoformat()
            else:
                start = _latest(issue['done_at'], job.get('turn_at') if job else None) or now
            if _seconds(start, now) > 0:
                start = now
            self.db.execute('UPDATE issues SET wait_from=? WHERE id=?', (start, issue['id']))
        if _seconds(now, start) >= WAIT_SECONDS:
            self.db.execute("UPDATE issues SET status='open', wait_from=NULL WHERE id=?", (issue['id'],))
            self._enqueue(issue, now)

    def _enqueue(self, issue, now):
        attempt = self.db.execute("SELECT count(*) FROM todos WHERE issue_id=? AND status!='cancelled'", (issue['id'],)).fetchone()[0] + 1
        instruction = INSTRUCTIONS.get(issue['kind'], DEFAULT_INSTRUCTION)
        if attempt >= ESCALATE_AT:
            instruction = f'第 {attempt} 次待办，{ESCALATION}。原要求：{instruction}'
        self.db.execute("INSERT INTO todos(issue_id, status, attempt, instruction, created_at) VALUES(?,'unclaimed',?,?,?)",
                        (issue['id'], attempt, instruction, now))

    def _close(self, issue_id, now):
        self.db.execute("UPDATE issues SET status='closed', closed_at=?, wait_from=NULL WHERE id=?", (now, issue_id))
        # The work is moot once the blocker is gone; a late 完成 still records its summary.
        self.db.execute("UPDATE todos SET status='cancelled' WHERE issue_id=? AND status IN ('unclaimed','claimed')", (issue_id,))

    def _issue(self, row):
        row = dict(row)
        return dict(id=row['id'], job_id=row['job_id'], company=row['company'], title=row['title'], owner=row['owner'],
                    kind=row['kind'], label=LABELS.get(row['kind'], row['kind']), handler=row['handler'], status=row['status'],
                    evidence=row['evidence'], first_seen_at=row['first_seen_at'], last_seen_at=row['last_seen_at'],
                    done_at=row['done_at'], notified_at=row['notified_at'], episodes=row['episodes'],
                    thread=f"codex://threads/{row['owner']}" if row['owner'] else None)

    def _todo(self, row):
        issue = self._issue(self.db.execute('SELECT * FROM issues WHERE id=?', (row['issue_id'],)).fetchone())
        history = [dict(at=h['done_at'], summary=h['summary'], needs_user=bool(h['needs_user'])) for h in self.db.execute(
            "SELECT done_at, summary, needs_user FROM todos WHERE issue_id=? AND status='done' AND id<? ORDER BY id", (row['issue_id'], row['id']))]
        return dict(id=row['id'], issue_id=row['issue_id'], status=row['status'], attempt=row['attempt'],
                    instruction=row['instruction'], created_at=row['created_at'], claimed_at=row['claimed_at'],
                    history=history, **{k: issue[k] for k in ('job_id', 'company', 'title', 'owner', 'kind', 'label', 'evidence', 'thread')})


# ---- Chinese copy for the controller ------------------------------------------
def user_request_message(request):
    """Public request only; audit sources and request identity stay in the ledger."""
    action = request.get('action')
    if not isinstance(action, str) or not action.strip():
        raise ValueError('user request requires a concrete action')
    text = action.strip()
    evidence = request.get('evidence')
    # Preserve the actual decision's substance when provided, without exposing
    # paths, source lists, diagnostics or internal semantic identifiers.
    substance = evidence.get('substance') if isinstance(evidence, dict) else None
    if isinstance(substance, str) and substance.strip() and substance.strip() not in text:
        text += '\n' + substance.strip()
    return text


def handoff_message(issue):
    """Event text asking the controller to relay one 待用户处理 record verbatim."""
    where = f"{issue.get('company') or '未知公司'} · {issue.get('title') or '未知职位'}（job_id {issue.get('job_id')}）"
    return (f"{HANDOFF_RULE}岗位：{where}。卡在：{issue['label']}，{issue.get('evidence') or '无说明'}。"
            f"原会话：{issue.get('thread') or '无'}")


def wake_message(count, *, scope=None, controller=None):
    command = ['.venv/bin/python', '-m', 'pipeline.cli', '待办']
    if scope is not None:
        command += ['--scope', str(scope)]
    if controller is not None:
        command += ['--controller', controller]
    command.append('下一条')
    return f'本批有 {count} 条待办，请处理到队列为空。\n对用户提问、接收补答和报告结果时，遵循 AGENTS.md「与用户沟通」。\n\n领取：\n{shlex.join(command)}'


def describe_todo(todo):
    where = f"{todo.get('company') or '系统'} · {todo.get('title') or '—'}（job_id {todo.get('job_id')}）" if todo.get('job_id') is not None else '系统（无岗位）'
    lines = [f"待办 编号：{todo['id']}", f"问题记录：{todo['issue_id']}（第 {todo['attempt']} 次待办）",
             f'岗位：{where}', f"阻碍类别：{todo['kind']}（{todo['label']}）",
             f"原会话：{todo.get('thread') or '无'}", f"证据：{todo.get('evidence') or '无'}", f"要求：{todo['instruction']}"]
    if todo['history']:
        lines.append('历次处理：')
        lines += [f"  {i}. {h['at']}：{h['summary']}" + ('（转给用户）' if h['needs_user'] else '') for i, h in enumerate(todo['history'], 1)]
    return '\n'.join(lines)

# Version 2 is the active interface; old tables are retained as migration evidence.
LegacyBlockerLedger = BlockerLedger
from pipeline.decision_queue import DecisionQueue as BlockerLedger
