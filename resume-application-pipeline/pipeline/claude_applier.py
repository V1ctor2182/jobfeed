"""Launch, observe, stop and restart one Claude CLI background Applier per job.

A launch is recorded before any process starts. An unknown launch result is reconciled against
`claude agents --json --all` by its unique name, never launched again. Restart uses `claude attach`,
because `claude --bg --resume <id> <message>` on a stopped session starts a copy and drops the message.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from pipeline.applier_mcp import TOOLS
from pipeline.applier_runtime import DEFAULT_MODEL, MAX_DRAFT, STRONG_MODEL, ApplierRuntime
from pipeline.model_routing import is_strong_ats

PIPELINE = Path(__file__).resolve().parents[1]
MCP_TIMEOUT_MS = 86_400_000
ALLOWED_TOOLS = ' '.join(f"mcp__applier__{tool['name']}" for tool in TOOLS)
# Questions go to the user through ask_user; a built-in question would wait unseen in a background session.
DISALLOWED_TOOLS = 'Bash Edit Write NotebookEdit AskUserQuestion'
BROWSER_OS = {'Linux': 'Linux', 'Darwin': 'macOS', 'Windows': 'Windows'}.get(platform.system(), platform.system())
APPLY_BROWSER = '.pipeline/apply-browser.json'
_DEVICE_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
RESTART_MESSAGE = ('The application backend restarted this session. Call tabs_context_mcp and find your registered tab. '
                   'If you were waiting on ask_user or report_blocker, call it again with the same arguments; it returns the saved reply. '
                   'Then continue from where you stopped.')
ESCALATION_MESSAGE = ('The application backend switched this session to a stronger model. Call tabs_context_mcp and find your registered tab. '
                      'Retry the field "{field}". These methods already failed: {attempts}. Use up to three different methods. '
                      'If all three fail, call report_blocker again. Then continue from where you stopped.')
_BACKGROUNDED = re.compile(r'backgrounded\s*·\s*([0-9a-f]{8})\b')
_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS applier_launches(
 launch_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
 background_id TEXT, session_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
'''
# Observed CLI state for a recorded launch. A copy under the same name is never adopted.
# `done` and `failed` mean the session ended its turn and has no live task; both restart like a stopped one.
CLI_STATES = {'working': 'running', 'blocked': 'running', 'done': 'finished', 'failed': 'failed', 'stopped': 'stopped'}
ENDED = frozenset({'stopped', 'finished', 'failed'})
# Stopped to free its processes while it waits, and brought back under the same session ID. Never in ENDED, so readers
# that ask whether an Applier ended for good keep its questions open and raise no alert.
PARKED = 'parked'
RESTARTABLE = ENDED | {PARKED}
WAKEABLE = (RESTARTABLE - {'stopped'}) | {'running'}
# The CLI refused to start the session, for example in a folder Claude does not trust. No session exists.
LAUNCH_FAILED = 'launch_failed'
# The input box's status line: older CLIs end it with the mode hint, 2.1.319 with the mode itself.
READY_MARKERS = ('shift+tab to cycle', '⏵⏵', '? for shortcuts')
PERMISSION_MARKERS = ('Do you want to proceed', 'Do you want to make this edit', 'Yes, and don', 'No, and tell Claude')
SESSION_MISSING = ('Job {job_id}: its Applier session {name} is gone from claude agents. Nothing was restarted or resent, '
                   'and the job keeps its slot. It needs you.')


def browser_rule(root):
    """The prompt line that picks the Claude in Chrome browser: the applying browser once its deviceId is recorded,
    otherwise this computer's."""
    try:
        device = json.loads((Path(root) / APPLY_BROWSER).read_text()).get('device_id')
    except (OSError, ValueError, AttributeError):
        device = None
    if isinstance(device, str) and _DEVICE_ID.fullmatch(device):
        return (f'Before your first browser action, call select_browser with deviceId {device}, the applying browser. '
                'Never use another browser and never ask which browser to use.')
    return (f'If Claude in Chrome reports that several browsers are connected, call select_browser with the deviceId of the {BROWSER_OS} browser, '
            'the one on this computer, and never ask which browser to use.')


class InventoryUnavailable(RuntimeError, OSError):
    """`claude agents` could not report its sessions. The worker skips that cycle's Claude work on an OSError,
    and a wake that fails on a RuntimeError releases its message, so this is both."""


PROMPT = '''You are the Claude Applier for Jobfeed job {job_id}. Application page: {url}
Your materials are in {materials}. Read only what you need.

Rules:
- Use only the claude-in-chrome tools for the page, in this session's own tab group. Never read or change another tab.
- Close tabs only when a tool result tells you to, and then exactly as it says. While you close them, a missing tab is expected: never report it and never open a page again.
- Never click Submit or any final submit button unless request_submit authorizes it, and then click it exactly once. Read your materials with the Read tool. Save results only through the applier tools.
- If a tool result tells you to end your turn, end it at once.
- If a user reply tells you to skip this posting or stop applying to it, call skip_job with that reply's request_id and reply_version, then do what it returns.
- Before every browser action, call tabs_context_mcp and confirm that your registered tab is still there.
- {browser_rule}
- If your registered tab is gone, never open the URL again and never treat a new page as your draft. Call report_blocker with blocker_type tab_missing.
- If the posting page shows that the job is gone, in any wording, such as closed, expired, filled, removed, no longer available, no longer accepting applications, "Job not found", or a not found (404) page where the posting was, call report_closed with the page's exact sentence, then do what it returns. It is never page_changed, and never a question for the user.
- If the page shows a CAPTCHA or any bot check, such as reCAPTCHA, hCaptcha or a Cloudflare "Verify you are human" page, before or after Submit, never try to solve it, click through it or get around it. Call report_blocker with blocker_type captcha and the page URL in page_url, then do what it returns.
- If a Workday page asks you to sign in or create an account, never type an email or password, and never use Forgot Password. Call account_step with the page's exact URL, then do what it returns.
- If any other employer or applicant-tracking site needs an account before you can apply, make one without asking. Choose its create account form, or its sign-in form when the site says the email already has an account. Type the profile's email yourself, then call fill_password with the page's exact URL: a program types the user's shared password into the password fields. Never see, type or ask for a password, and never use Forgot Password. Then finish the form, and use email_code or email_link when the site emails a code or a confirmation link. Never sign in with Google, Microsoft, Apple, LinkedIn, Facebook or any other personal account, and never grant such an account access: when a site offers nothing else, or wants a code by text message or phone call, call report_blocker with blocker_type user_only_check.
- If a page asks for a verification code that the site sent to the user's email, before or after Submit, call email_code with the page's exact URL, type the code it returns and continue. It is never a question for the user, and after Submit it is never submit_unknown: the application is not complete until the code is entered. A code sent by text message or made by an authenticator app still needs the user: call report_blocker with blocker_type user_only_check.
- Work authorization and sponsorship are never a question for the user. Answer them from My Profile's work authorization, sponsorship and visa status answers. When a form combines or rewords them, work the answer out from those three: for example, "authorized to work without sponsorship now or in the future" is No when the sponsorship answer is Yes. When a saved answer to another wording contradicts My Profile, My Profile wins.
- If My Profile's sponsorship answer is Yes and the posting or the form states in plain words that the employer will not sponsor a visa for this role, or accepts only applicants who need no sponsorship now or in the future, do not apply: call report_no_sponsorship with that exact sentence, then do what it returns. A question that only asks whether the user needs sponsorship is not such a statement, so answer it and continue.
- After Submit, never ask the user whether it went through. When the outcome is unclear, report_blocker with blocker_type submit_unknown waits for a program that reads the employer's confirmation email and answers it.
- If the browser tools say the extension is not connected, call report_blocker with blocker_type browser_disconnected. If your tab now shows a different page than the form, use page_changed. If a tool says another session controls the tab, use foreign_owner.

Steps:
1. Call get_notes with the page's host name. Follow the notes. When a listed helper matches a widget, use get_helper for that widget first.
2. Open the page in your own tab group. Call register_tab with its tab ID and URL. If your job folder has a resume catalog, call list_resumes and select_resume once, and upload the exact file it returns.
3. Fill each field the normal way first. If that fails, retry with a different method, at most twice, such as the computer tool and then a script through javascript_tool. If a retry works, call save_note. If all three attempts fail, call report_blocker with blocker_type broken_component, the field, the three methods, the error, and the relevant page structure.
4. {answers}
5. Tick any checkbox the form requires before you can continue, such as a privacy policy, applicant data consent or terms acknowledgement, without asking. Leave optional opt-ins, such as text messages, marketing or referrals, empty. {signature} After filling a field, read it back and call record_readback. When the value came from a reply, pass its request_id and reply_version. After each section, call record_progress.
6. Before the final Submit, call request_submit. Follow its answer exactly: click Submit only when it says you are authorized, and click it exactly once.
7. After a click, wait for the page to settle. If it shows an explicit acknowledgement, take a screenshot with the computer tool, then read the page with get_page_text, then call record_confirmation, one call at a time. If record_confirmation refuses, follow its error and keep the tab open. Once record_submit_outcome with submitted returns recorded, close every tab in your own tab group as it says, the job listing page and any popup included, and call record_tab_closed. Never close a tab before that. Otherwise record not_submitted or unknown as the tools describe.
'''
_PROFILE_FACT = ("For a contact or identity fact (name, email, phone, address, LinkedIn, GitHub, portfolio), call lookup_profile with the field name and "
                 "fill the profile value, even when the form arrives prefilled with something different.")
_COMMON_LOOKUP = ("When a question asks one of My Profile's common questions in any wording, such as work authorization, sponsorship, visa status, relocation, "
                  "working onsite, preferred locations, start date, text message consent, or the self-identification questions about gender, race, Hispanic or "
                  "Latino, veteran status or disability, or any other the common_question choices list, always call lookup_answer with the question and "
                  "common_question set to its key, and select the form choice that means the same.")
_OTHER_LOOKUP = ("For any other fact, call lookup_profile with the field name, and for any other application question call lookup_answer with the exact question. "
                 "Use an approved answer exactly. When the form offers fixed choices that word it differently, select the choice that means the same. "
                 "If it lists related answers instead, and one of them asks the same thing about the user in other words, fill that answer without asking. "
                 "This never covers a question about this employer or role, a question that needs a written answer, or an answer you would have to change to fit.")
_HOW_TO_ASK = ("Then call ask_user with the exact question and the field. Ask one question per call, so each reply answers one field. For a form question, also "
               "pass form_question with the label copied exactly from the form, and applies_to: every_company for any fact about the user, such as work "
               "authorization, sponsorship, relocation, working onsite, start date, locations or text message consent, even when the question names this "
               "employer's office, city, schedule or messages; this_company only for a question about this employer or role, such as why this company, which "
               "team, past work at this company, offer deadlines, or consent to share your data with this company's partners. The reply is then saved, so no "
               "Applier asks it again. Offer fixed choices only through options, and never number them in the question. The user answers with buttons, "
               "so never ask an open question: for a free-text field, pass suggestions with the two to four answers the user is most likely to give.")
_SHORT_ANSWER = ("A short-answer question is any free-text field about the user's experience, projects, motivation, interests or goals, why this company or "
                 "role, a cover letter, or anything else to add. If lookup_answer returns an approved answer to that exact question, fill it.")
_DRAFT_SOURCES = ("Before your first draft for this job, call get_draft_feedback once and follow the user's general preferences in it, such as tone, length or "
                  "words to avoid; ignore feedback that was about one other question only. Build the draft only from the profile, resumes, approved answers "
                  "and posting, and never add a fact they do not state.")
_DRAFT_VOICE = ("Write the draft as the user would write it: first person, plain everyday words and contractions, and sentences of mixed length. Use concrete "
                "specifics from the materials, such as a named project, what was built or a number, instead of adjectives. For why this company, name something "
                "specific from the posting or the company's product. Do not restate the question. Never use clichés or filler such as passionate, excited to "
                "leverage, thrilled, I am writing to, in today's fast-paced world, synergy, delve, tapestry, cutting-edge or a testament to. Use no em dashes, "
                f"and no bullet lists unless the form asks for them. Stay within the form's length limit and under {MAX_DRAFT} characters.")
_FILL_REPLY = 'ask_user waits for the user. Fill the reply exactly.'
# Step 4 and the signature rule, by whether the user's auto submit mode was on at launch. With it on, the Applier decides what it can.
ANSWERING = {
    False: {'answers': ' '.join((
        _PROFILE_FACT,
        "If the profile lacks it, or does not say which of its values the field takes (such as Full name when it lists a legal and a preferred name), "
        "call ask_user without form_question and pass profile_field with the field's label in plain words, such as Full name or GitHub, so the reply is "
        "saved to My Profile and no Applier asks it again. These are never saved as answers.",
        _COMMON_LOOKUP,
        "If it has no answer, call ask_user with common_question: the user picks from My Profile's choices and the reply is saved to My Profile for every "
        "company. This holds even when the question names this employer's office, city, schedule or messages.",
        _OTHER_LOOKUP,
        "Ask the user only when no approved answer, related answer, profile fact or your materials cover the question, when it asks about this employer or "
        "role and has no approved answer of its own, or when it needs a written answer that has none.",
        _HOW_TO_ASK, _SHORT_ANSWER,
        'Otherwise always pass draft with the answer you propose; never ask a short-answer question without one.',
        _DRAFT_SOURCES,
        'When they say little, still draft from what they do say, and name in the question the one fact that would make the answer stronger.',
        _DRAFT_VOICE,
        'When ask_user returns feedback instead of a reply, the user asked for changes: rewrite the draft to follow the feedback and call ask_user again with '
        'the same question, form_question, applies_to and field and the new draft. Keep going until the user approves a draft or writes their own, and fill '
        'nothing in that field until then.',
        _FILL_REPLY)),
        'signature': 'Still ask before typing or drawing a signature.'},
    True: {'answers': ' '.join((
        'The user turned on auto submit mode, so answer every question yourself and ask only when it is really necessary. Nothing you fill waits for approval.',
        _PROFILE_FACT,
        'When the profile has several values for the field, such as a legal and a preferred name, fill the profile value for that exact label when it has '
        'one; otherwise a plain name field takes the profile\'s Full name, and the legal name goes only into a field that asks for a legal name.',
        _COMMON_LOOKUP, _OTHER_LOOKUP, _SHORT_ANSWER,
        'When nothing approved answers a question, decide it from My Profile, the approved answers, your resumes and the posting, and choose the truthful, '
        'most reasonable option. A question about the user\'s past with this employer, such as working there before or knowing someone there, is No unless '
        'the materials show it. Certifications and licenses the materials do not list are None, or N/A when the form offers it; a question about driving takes '
        'the driver\'s license from the profile. How you heard about the job is where this job was found, the company\'s own careers site or the job board in '
        'its address, never a referral. An optional or voluntary question takes the profile\'s self-identification answer, or decline to answer when it has '
        'none. A question you can answer from known facts, such as spelling the user\'s name phonetically or counting years of experience from the resumes, '
        'gets that answer. Leave an optional field empty when nothing supports an answer. Write each short answer yourself and fill it without asking.',
        _DRAFT_SOURCES, 'When they say little, still write from what they do say.', _DRAFT_VOICE,
        'After you fill an answer you decided, call record_answer with the form\'s label as question, the exact answer, and applies_to: every_company '
        'for any fact about the user, this_company for a question about this employer or role. For a common question pass common_question instead. '
        'Later Appliers then reuse it. Never record a contact or identity fact. If record_answer says the user already answered, fill '
        'their answer instead. The review summary shows the user every answer you decided.',
        'Ask the user only for a fact that no material states and that a guess could make false in a way that matters, such as a license number, a '
        'criminal history or other legal attestation the profile does not answer, or an exact past salary; for a signature other than typing the profile\'s '
        'full name; or for a choice that commits the user beyond applying, such as a relocation agreement or a paid assessment.',
        _HOW_TO_ASK,
        'For a contact or identity fact the profile lacks, pass profile_field with the field\'s label in plain words instead of form_question, and for a common '
        'question pass common_question, so the reply is saved to My Profile.',
        _FILL_REPLY)),
        'signature': 'Where the form asks you to type your name as a signature, type the profile\'s full legal name. Still ask before drawing a signature.'},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


CLAUDE_SESSION_VARIABLES = {'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION',
                            'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH'}


def boot_time():
    """When this machine booted, in seconds since the epoch, or None where /proc/stat does not say."""
    try:
        return next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    except (OSError, StopIteration, ValueError):
        return None


def has_process(record):
    """Whether the session still has a process. One that ended its turn can stay listed done with a live process."""
    return record['state'] == 'running' or (record['state'] in {'finished', 'failed'} and bool(record.get('pid')))


def never_started(record):
    """A launch whose `claude --bg` produced no session. Only these start again."""
    return record['state'] == LAUNCH_FAILED or (record['state'] == 'unknown' and record['background_id'] is None)


def clean_env(environ=None):
    """The launcher's environment without Codex host variables or the variables of a Claude Code session that started
    the Dashboard or worker. A daemon that `claude --bg` starts inherits it."""
    return {k: v for k, v in (os.environ if environ is None else environ).items()
            if not k.upper().startswith(('CODEX', 'CLAUDE_CODE_MESSAGING_')) and k not in CLAUDE_SESSION_VARIABLES}


def user_mcp_servers(home=None):
    """Names of the MCP servers in the user's own Claude config. An Applier denies them all: it needs only its applier
    server and Claude in Chrome, and each one it loaded started a process of its own (2026-11-17: 25 chrome-devtools-mcp
    processes, 1.3 GB, on a machine that then ran out of memory). `--strict-mcp-config` would drop Claude in Chrome too."""
    try:
        servers = json.loads((Path(home or Path.home()) / '.claude.json').read_text()).get('mcpServers') or {}
    except (OSError, ValueError, AttributeError):
        return []
    return sorted(name for name in servers if name != 'applier')


class ClaudeApplierLauncher:
    def __init__(self, root, *, claude='claude', python=sys.executable, run=subprocess.run, attach=None):
        self.root = Path(root).resolve()
        self.store = ApplierRuntime(self.root)
        self.claude, self.python, self.run = claude, python, run
        self.attach = attach or self._attach_and_send

    def _db(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.store.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.executescript(_SCHEMA)
        added = (('pid', 'INTEGER'), ('restart_expected', 'INTEGER NOT NULL DEFAULT 0'),
                 ('idle_since', 'TEXT'), ('parked_at', 'TEXT'), ('parked_request', 'TEXT'))
        columns = {row[1] for row in db.execute('PRAGMA table_info(applier_launches)')}
        if any(name not in columns for name, _ in added):
            # Another connection may be adding the same columns, so look again under the write lock.
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(applier_launches)')}
        for name, kind in added:
            if name not in columns:
                db.execute(f'ALTER TABLE applier_launches ADD COLUMN {name} {kind}')
        db.commit()
        return db

    def _record(self, launch_id):
        with closing(self._db()) as db:
            row = db.execute('SELECT * FROM applier_launches WHERE launch_id=?', (launch_id,)).fetchone()
        if row is None:
            raise ValueError('unknown launch')
        return dict(row, config=json.loads(row['config']))

    def _update(self, launch_id, **fields):
        with closing(self._db()) as db, db:
            db.execute(f"UPDATE applier_launches SET {','.join(f'{k}=?' for k in fields)},updated_at=? WHERE launch_id=?",
                       (*fields.values(), _now(), launch_id))

    def launch(self, job_id, *, scope, url, materials, work_dir, jobfeed_db=None, dashboard_url=None, launch_id=None, evidence_dir=None, job_url=None, company=None):
        """Start one Applier for the job. A job that already has a launch gets that launch back."""
        with closing(self._db()) as db:
            existing = db.execute('SELECT launch_id FROM applier_launches WHERE job_id=?', (job_id,)).fetchone()
        if existing is not None:
            return self.retry(existing['launch_id'])
        if self.store.control('draining'):
            raise RuntimeError('Claude Appliers are draining, so no new Applier starts')
        # A launch that crashed after binding left an owner but no launch row; adopt that owner instead of a new one.
        bound = next((job['owner'] for job in self.store.jobs() if job['job_id'] == job_id), None)
        if bound is not None and launch_id is not None and bound != launch_id:
            raise ValueError('job already has a different Applier owner')
        launch_id = launch_id or bound or uuid.uuid4().hex
        model = STRONG_MODEL if is_strong_ats(url) else DEFAULT_MODEL
        self.store.bind(job_id, launch_id, scope, model)
        work_dir, materials = Path(work_dir).resolve(), [Path(m).resolve() for m in materials]
        work_dir.mkdir(parents=True, exist_ok=True)
        mcp_config = work_dir / 'applier-mcp.json'
        mcp_config.write_text(json.dumps({'mcpServers': {'applier': {
            'command': self.python, 'args': ['-m', 'pipeline.applier_mcp', '--root', str(self.root), '--job', str(job_id), '--owner', launch_id,
                                             *(['--jobfeed-db', str(Path(jobfeed_db).resolve())] if jobfeed_db else []),
                                             *(['--dashboard-url', dashboard_url] if dashboard_url else []),
                                             *(['--evidence-dir', str(Path(evidence_dir).resolve()), '--job-url', job_url or url] if evidence_dir else []),
                                             *(['--company', company] if company else [])],
            'env': {'PYTHONPATH': str(PIPELINE)}, 'timeout': MCP_TIMEOUT_MS}}}, indent=2))
        settings = work_dir / 'applier-settings.json'
        guard = Path(__file__).resolve().with_name('applier_guard.py')
        settings.write_text(json.dumps({'hooks': {'PreToolUse': [{'matcher': 'mcp__claude-in-chrome__.*', 'hooks': [
            {'type': 'command', 'command': f'{self.python} -I {guard}'}]}]},
            'deniedMcpServers': [{'serverName': name} for name in user_mcp_servers()]}, indent=2))
        name = f'applier-{job_id}-{launch_id[:8]}'
        prompt = PROMPT.format(job_id=job_id, url=url, materials=', '.join(map(str, materials)) or 'none', browser_rule=browser_rule(self.root),
                               **ANSWERING[self.store.auto_submit()['enabled']])
        argv = [self.claude, '--bg', '--chrome', '--model', model, '--settings', str(settings), '--mcp-config', str(mcp_config), '--allowedTools', ALLOWED_TOOLS, '--disallowedTools', DISALLOWED_TOOLS]
        for path in materials:
            argv += ['--add-dir', str(path)]
        argv += ['-n', name, prompt]
        config = {'cwd': str(work_dir), 'argv': argv, 'url': url}
        with closing(self._db()) as db, db:
            db.execute('INSERT INTO applier_launches(launch_id,job_id,name,config,background_id,session_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (launch_id, job_id, name, json.dumps(config), None, None, 'requested', _now(), _now()))
        return self._start(launch_id)

    def _start(self, launch_id):
        """Run the stored `claude --bg` command once. A refusal is recorded with the CLI's reason; a timeout stays unknown."""
        record = self._record(launch_id)
        self._update(launch_id, state='requested')
        try:
            result = self.run(record['config']['argv'], cwd=record['config']['cwd'], env=clean_env(), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            self._update(launch_id, state='unknown')
            return self.reconcile(launch_id)
        output = _ANSI.sub('', result.stdout + result.stderr)
        match = _BACKGROUNDED.search(output)
        if match:
            self._update(launch_id, state='launched', background_id=match.group(1))
            return self.reconcile(launch_id)
        self._update(launch_id, state='unknown')
        record = self.reconcile(launch_id)
        if record['state'] != 'unknown' or result.returncode == 0:
            return record
        reason = next((line.strip() for line in output.splitlines() if line.strip()), f'claude exited with code {result.returncode}')[:300]
        self._update(launch_id, state=LAUNCH_FAILED)
        self.store.alert(record['job_id'], LAUNCH_FAILED, reason)
        return self._record(launch_id)

    def retry(self, launch_id):
        """Start again a launch that never produced a session. Any other launch is only reconciled."""
        record = self.reconcile(launch_id)
        if never_started(record):
            if self.store.control('draining'):
                raise RuntimeError('Claude Appliers are draining, so no new Applier starts')
            return self._start(launch_id)
        return record

    def agents(self):
        try:
            result = self.run([self.claude, 'agents', '--json', '--all'], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InventoryUnavailable(f'claude agents --json did not run: {exc}') from exc
        if result.returncode != 0:
            raise InventoryUnavailable('claude agents --json failed')
        return json.loads(result.stdout)

    def reconcile(self, launch_id):
        """Match the launch to the CLI inventory by its unique name and record what the CLI reports.

        A session the CLI lists as stopped is parked while its parked_at is set. idle_since records when the launcher first
        saw the session idle with a live process.
        """
        record = self._record(launch_id)
        matches = [a for a in self.agents() if a.get('name') == record['name'] and a.get('cwd') == record['config']['cwd']]
        if not matches:
            # A session that ran has a background ID, so `unknown` keeps its slot and is never started or restarted again.
            if record['state'] in {'running', PARKED}:
                self.store.alert(record['job_id'], 'session_missing', SESSION_MISSING.format(job_id=record['job_id'], name=record['name']))
            if record['state'] in {'requested', 'unknown', 'launched', 'running', PARKED}:
                self._update(launch_id, state='unknown')
            return self._record(launch_id)
        if len(matches) > 1 or (record['session_id'] and matches[0].get('sessionId') != record['session_id']):
            self._update(launch_id, state='duplicate')
            return self._record(launch_id)
        agent = matches[0]
        pid = agent.get('pid')
        if pid and record.get('pid') and pid != record['pid'] and not record.get('restart_expected'):
            self.store.alert(record['job_id'], 'process_restarted', f"process {record['pid']} was replaced by {pid} without a launcher restart")
        state = CLI_STATES.get(agent.get('state'), 'unknown')
        if state == 'stopped' and record['parked_at']:
            state = PARKED
        cli_state = agent.get('state')
        # The CLI can keep listing a session as working after its turn ended; an idle process waits for input.
        if cli_state == 'working' and agent.get('status') == 'idle':
            cli_state = 'blocked'
        # After a reboot the CLI keeps listing dead sessions as working or blocked, without a pid. A session just launched
        # is also listed without a pid for a moment, so only one that started before this boot counts as ended.
        booted = boot_time()
        if state == 'running' and not pid and booted is not None and agent.get('startedAt', booted * 1000) < booted * 1000:
            state, cli_state = 'finished', 'done'
        idle = cli_state != 'working' and has_process({'state': state, 'pid': pid})
        fields = dict(background_id=agent.get('id') or agent['sessionId'][:8], session_id=agent['sessionId'], state=state,
                      idle_since=(record['idle_since'] or _now()) if idle else None, pid=pid)
        if pid:
            fields.update(restart_expected=0)
        self._update(launch_id, **fields)
        return dict(self._record(launch_id), cli_state=cli_state, pid=agent.get('pid'))

    def stop(self, launch_id):
        """Stop the session on purpose. It stays stopped until someone restarts it, and a parked one stops being parked."""
        return self._stop(launch_id, parked_at=None, parked_request=None)

    def park(self, launch_id, awaiting=None):
        """Stop an idle session to free its processes, keeping its session ID, transcript and tabs.

        awaiting is the open request it waits on. The supervisor restarts it once that request closes, and wake() restarts
        it for any message. The intent is written before `claude stop`, so a pass that dies in between leaves a marker
        that reconcile ignores while the CLI still lists the session as live.
        """
        return self._stop(launch_id, parked_at=_now(), parked_request=awaiting)

    def _stop(self, launch_id, **marker):
        record = self.reconcile(launch_id)
        self._update(launch_id, **marker)
        if has_process(record):
            self.run([self.claude, 'stop', record['background_id']], capture_output=True, text=True, timeout=60)
        return self.reconcile(launch_id)

    def restart(self, launch_id, message=RESTART_MESSAGE, *, model=None):
        """Bring a stopped, ended or parked Applier back under the same session ID and give it a fixed message, optionally
        on another model. Once it runs, it is no longer parked."""
        record = self.reconcile(launch_id)
        if record['state'] not in RESTARTABLE:
            raise ValueError(f"only a stopped or finished Applier can be restarted, this one is {record['state']}")
        self._update(launch_id, restart_expected=1, idle_since=None)
        self.attach(record['background_id'], message, record['config']['cwd'], model=model)
        record = self.reconcile(launch_id)
        if record['state'] != 'running':
            raise RuntimeError(f"restart did not bring the Applier back, it is {record['state']}")
        self._update(launch_id, parked_at=None, parked_request=None)
        return dict(record, parked_at=None, parked_request=None)

    def wake(self, launch_id, message):
        """Give an idle Applier a fixed message: restart it if it ended its own turn or was parked, or type into it if it
        waits for input.

        A stopped session was stopped on purpose and is never restarted here, and nothing is woken while draining.
        """
        if self.store.control('draining'):
            return 'draining'
        record = self.reconcile(launch_id)
        if record['state'] == 'stopped':
            return 'stopped'
        if record['state'] in RESTARTABLE:
            # Its waiting ask_user or report_blocker call died with the process, so it must call it again.
            self.restart(launch_id, f'{RESTART_MESSAGE}\n\n{message}' if record['state'] == PARKED and record['parked_request'] else message)
            return 'restarted'
        if record.get('cli_state') == 'blocked':
            self._update(launch_id, idle_since=None)
            self.attach(record['background_id'], message, record['config']['cwd'])
            return 'messaged'
        return 'busy'

    def escalate(self, launch_id, request):
        """Restart the Applier on the stronger model to retry the blocked field, then mark the escalation sent."""
        attempts = ', '.join((request.get('evidence') or {}).get('attempts') or []) or 'not recorded'
        record = self.restart(launch_id, ESCALATION_MESSAGE.format(field=request['field'], attempts=attempts), model=STRONG_MODEL)
        self.store.escalation_sent(request['id'], STRONG_MODEL)
        return record

    def launches(self):
        with closing(self._db()) as db:
            return [row['launch_id'] for row in db.execute('SELECT launch_id FROM applier_launches ORDER BY created_at')]

    def drain(self):
        """Stop new launches, stop Appliers waiting at a safe point, and report the rest. Call again until working is empty."""
        self.store.set_control('draining', datetime.now(timezone.utc).isoformat())
        waiting_jobs = {r['job_id'] for r in self.store.requests() if r['state'] == 'open'}
        result = {'stopped': [], 'working': [], 'ended': []}
        for launch_id in self.launches():
            record = self.reconcile(launch_id)
            if record['state'] == 'running' and record['job_id'] in waiting_jobs:
                self.stop(launch_id)
                result['stopped'].append(record['job_id'])
            elif record['state'] == 'running':
                result['working'].append(record['job_id'])
            else:
                result['ended'].append(record['job_id'])
        return result

    def resume_dispatch(self):
        self.store.set_control('draining', None)

    def launch_for(self, job_id):
        with closing(self._db()) as db:
            row = db.execute('SELECT launch_id FROM applier_launches WHERE job_id=?', (job_id,)).fetchone()
        return None if row is None else row['launch_id']

    def _attach_and_send(self, background_id, message, cwd, *, model=None, ready_timeout=45):
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        env = dict(clean_env(), TERM='xterm-256color', COLUMNS='200', LINES='50')
        process = subprocess.Popen([self.claude, 'attach', background_id], stdin=slave, stdout=slave, stderr=slave,
                                   cwd=cwd, env=env, start_new_session=True)
        os.close(slave)
        screen = ''

        def wait_for(markers, timeout):
            nonlocal screen
            deadline = time.monotonic() + timeout
            while not any(marker in screen for marker in markers):
                if time.monotonic() > deadline or process.poll() is not None:
                    return False
                if select.select([master], [], [], 0.5)[0]:
                    try:
                        screen += _ANSI.sub('', os.read(master, 65536).decode(errors='replace'))
                    except BlockingIOError:
                        pass
            return True

        def type_line(text):
            os.write(master, text.encode())
            time.sleep(0.5)
            os.write(master, b'\r')

        try:
            if not wait_for(READY_MARKERS, ready_timeout):
                raise RuntimeError('claude attach did not show its prompt')
            # Typing Enter into an open permission dialog would answer it, so refuse instead.
            if any(marker in screen for marker in PERMISSION_MARKERS):
                raise RuntimeError('the Applier is showing a permission prompt; it needs the user, not a typed message')
            if model is not None:
                screen = ''
                type_line(f'/model {model}')
                if wait_for(('Switch model?',), 10):
                    os.write(master, b'\r')
                time.sleep(2)
            type_line(message)
            time.sleep(3)
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(10)
            os.close(master)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    launch = sub.add_parser('launch')
    launch.add_argument('--job', type=int, required=True)
    launch.add_argument('--scope', required=True)
    launch.add_argument('--url', required=True)
    launch.add_argument('--work-dir', type=Path, required=True)
    launch.add_argument('--material', type=Path, action='append', default=[])
    launch.add_argument('--jobfeed-db', type=Path)
    for command in ('status', 'stop', 'restart', 'retry'):
        sub.add_parser(command).add_argument('launch_id')
    sub.add_parser('drain')
    sub.add_parser('resume-dispatch')
    args = parser.parse_args(argv)
    launcher = ClaudeApplierLauncher(args.root)
    if args.command == 'drain':
        result = launcher.drain()
    elif args.command == 'resume-dispatch':
        launcher.resume_dispatch()
        result = {'draining': False}
    elif args.command == 'launch':
        result = launcher.launch(args.job, scope=args.scope, url=args.url, materials=args.material, work_dir=args.work_dir, jobfeed_db=args.jobfeed_db)
    else:
        result = {'status': launcher.reconcile, 'stop': launcher.stop, 'restart': launcher.restart, 'retry': launcher.retry}[args.command](args.launch_id)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
