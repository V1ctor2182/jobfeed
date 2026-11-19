"""Stdio MCP server for one Claude Applier. Every tool acts on the job and owner fixed at launch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading
from urllib.parse import urlsplit

from pipeline import applier_helpers
from pipeline.applier_runtime import BLOCKER_TYPES, DRAFT_FEEDBACK_LIMIT, MAX_DRAFT, MAX_OPTION, MAX_OPTIONS, RETRYABLE_SUBMIT, ApplierRuntime
from pipeline import claude_evidence, workday_accounts
from pipeline.confirmation_mail import NO_EMAIL
from pipeline.browser_broker import BrokerClient
from pipeline.application_profile import ProfileBridge
from pipeline.common_questions import CATALOG
from pipeline.coordination import CoordinationStore
from pipeline.application_rerun import RerunError

MAX_LINE = 65536
ESCALATED_INSTRUCTION = 'End your turn now. The backend restarts this session on a stronger model to retry this field.'
REDRAFT_INSTRUCTION = ('Rewrite the draft to follow this feedback, then call ask_user again with the same question, form_question, applies_to and field '
                      'and the new draft. Nothing was saved.')
RETRY_INSTRUCTION = 'Recheck your original tab by its tab ID, retry the field "{field}" with the reply above, then read the field back and call record_readback.'
CAPTCHA_INSTRUCTION = ('Do not try to solve or get around the check. Leave the tab exactly as it is and end your turn now. The backend sets this job '
                       'aside as CAPTCHA needed and starts other jobs; it tells you when the user has solved it.')
CLOSE_TABS = ('Now close every tab in your own tab group. Call tabs_context_mcp, never with createIfEmpty, and close each tab it lists with tabs_close_mcp: '
              'first any job listing page, popup or other tab opened for this job, then your registered form tab last. Never close a tab outside your group, '
              'and never open a page again. A missing tab is expected now, so never report it. Once tabs_close_mcp says the group is empty, or '
              'tabs_context_mcp says it no longer exists, call record_tab_closed.')
CAPTCHA_REQUEUE_INSTRUCTION = ('Do not try to solve or get around the check. Nothing was submitted, so the backend sets this job aside as CAPTCHA needed, '
                               'and once the user has solved it a new Applier fills the form again in a fresh tab. ' + CLOSE_TABS)
SKIPPED_INSTRUCTION = 'The user skipped this job, so never ask, fill or submit anything for it. ' + CLOSE_TABS
EMAIL_CONFIRMED_INSTRUCTION = ('The employer\'s confirmation email arrived and was saved as this job\'s submission evidence. '
                               'Call record_submit_outcome with outcome submitted and evidence naming that email, then do what it returns.')
NO_EMAIL_INSTRUCTION = ('No confirmation email from the employer arrived, so by the user\'s standing rule this application counts as not submitted. '
                        'Never click Submit again and never ask the user about it. Call record_progress with "not submitted". ')
NO_SPONSORSHIP_INSTRUCTION = 'The employer does not sponsor, so the user does not apply here. Never ask, fill or submit anything for this job. '
CODE_FOUND = ('Type this code into the page\'s code field now and continue. Never write the code into a note, a summary, record_progress or any other tool call.')
CODE_MISSING = ('No email with a code for this site arrived. If the page offers to send the code again, use that once, then call email_code again. '
                'If it still returns no code, call report_blocker with blocker_type user_only_check and the page URL in page_url.')
PASSWORD_NEXT = {
    'filled': 'The password is in the page\'s {fields} password field(s). Finish the form yourself: tick the agreements it requires and click its create account or sign in '
              'button. If the site then emails a code, call email_code; if it emails a link to confirm the account, call email_link. If the site says the '
              'email already has an account, switch to its sign-in form and call fill_password again. If it refuses the password on sign-in, call '
              'report_blocker with blocker_type user_only_check and say that the shared password does not open the existing account on this site.',
    'no_password_field': 'The page shows no password field yet. Open the create account or sign-in form, type the email if it asks for that first, and call '
                         'fill_password again with the page\'s exact current URL.',
    'personal_account': 'This page signs in to one of the user\'s personal accounts, where the shared password is never typed. If the site offers an account of '
                        'its own with an email and password, use that instead. Otherwise call report_blocker with blocker_type user_only_check.',
    'captcha': 'The page shows a CAPTCHA. Call report_blocker with blocker_type captcha and the page URL in page_url, then do what it returns.',
    'tab_not_found': '{reason} Call tabs_context_mcp and call fill_password once more with the tab\'s exact current URL.',
    'no_password': 'No shared account password is set. Call report_blocker with blocker_type user_only_check and ask the user to set it under Profile, Login.',
}
PASSWORD_FAILED = ('The account program could not fill the password: {reason} Call report_blocker with blocker_type user_only_check and the page URL in page_url.')
LINK_FOUND = ('Open this link in a new tab of your own tab group with navigate, read what the page says, then close that tab and continue on your form tab.')
LINK_MISSING = ('No email with a confirmation link for this site arrived. If the page offers to send it again, use that once, then call email_link again. '
                'If it still returns none, call report_blocker with blocker_type user_only_check and the page URL in page_url.')
TAB_LOST_INSTRUCTION = ('Nothing was submitted, so the backend puts this job back in the queue and a new Applier fills the form again from the start. '
                        'Do not open the page again. End your turn now.')


ACCOUNT_ALERTS = {
    'not_connected': 'The Workday account program cannot reach the applying browser. Start it with systemctl --user start jobfeed-apply-chromium.',
    'broker_unavailable': 'The Workday account program is not running. Start it with systemctl --user start jobfeed-browser-broker.',
    'no_password': 'No shared account password is set. Run python -m pipeline.cli accounts set-password.',
}


def contract(name, description, properties=None, required=()):
    return dict(name=name, description=description, inputSchema=dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False))


_TEXT = {'type': 'string', 'minLength': 1}
# every_company suits any fact about the user, even one asked about this employer's office or schedule; this_company suits questions about the employer or role.
ANSWER_SCOPES = {'every_company': 'global', 'this_company': 'company'}
COMMON = {item.key: item for item in CATALOG}
_COMMON_KEY = {'type': 'string', 'enum': sorted(COMMON),
               'description': 'Set when the question asks one of the common questions in My Profile, in any wording: '
                              + '; '.join(f'{item.key} = {item.question}' for item in CATALOG)}
TOOLS = [
    contract('ask_user', 'Ask the user one question that your profile and confirmed answers do not cover. Waits until the user replies, then returns the reply and its version. '
             'For a form question, pass form_question with its label copied exactly from the form, and applies_to: the reply is then saved as an approved answer, so no Applier asks it again. '
             'applies_to is every_company for any fact about the user, such as relocation, working onsite, start date or text message consent, even when the question names this employer\'s office, city or schedule, '
             'and this_company only for a question about this employer or role, such as why this company, which team, past work there or its offer deadlines. '
             'Every short-answer question needs draft: the answer you propose, in the user\'s own voice, built only from facts in the profile, resumes, approved answers and job posting, '
             f'at most {MAX_DRAFT} characters. The user approves it, writes their own answer, or asks for changes. A returned reply is the one to fill. '
             'When it returns feedback instead of a reply, the user asked for changes and nothing was saved: rewrite the draft to follow the feedback and ask again with the new draft. '
             'When the form offers fixed choices (radio buttons, a dropdown, checkboxes), pass options with each choice copied exactly from the form: '
             'the user picks one with a button, or types another answer. Only a reply that is one of the options is saved. Never number the choices in the question. '
             'The user answers only with buttons, so never ask an open question: for a free-text field, pass suggestions, '
             'two to four answers the user is most likely to give, taken from the profile, resumes, approved answers and posting, such as the likely value of a fact and None. '
             'Each is a button, and Other lets the user type a different answer, which is saved too. '
             'A question needs options, suggestions or a draft. common_question fills in options only when My Profile has fixed choices for it. '
             'For a common question in My Profile, such as work authorization, sponsorship, relocation or working onsite, in any wording, '
             'pass common_question instead of form_question and applies_to: the user picks from My Profile\'s choices and the reply is saved to My Profile for every company. '
             'For a contact or identity fact the profile lacks, or when the profile does not say which of its values a field takes '
             '(such as Full name when it lists a legal and a preferred name), pass profile_field with the field\'s label in plain words, '
             'such as Full name or GitHub: the reply is saved to My Profile, so no Applier asks it again.',
             {'question': _TEXT, 'field': _TEXT, 'form_question': _TEXT, 'applies_to': {'type': 'string', 'enum': sorted(ANSWER_SCOPES)}, 'draft': _TEXT,
              'options': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': MAX_OPTION}, 'minItems': 2, 'maxItems': MAX_OPTIONS, 'uniqueItems': True},
              'suggestions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': MAX_OPTION}, 'minItems': 2, 'maxItems': 4, 'uniqueItems': True},
              'common_question': _COMMON_KEY, 'profile_field': _TEXT},
             ['question']),
    contract('report_blocker', 'Report that you cannot continue. For broken_component, list exactly the three different methods you tried. Waits until the user replies, then returns the reply and what to do next. '
             'For captcha, any CAPTCHA or bot check before or after Submit, pass page_url: it returns at once and says whether to close your tabs, then you end your turn. '
             'For tab_missing before any Submit attempt, it also returns at once, and you end your turn.',
             {'blocker_type': {'type': 'string', 'enum': sorted(BLOCKER_TYPES)}, 'summary': _TEXT, 'field': _TEXT,
              'attempts': {'type': 'array', 'items': _TEXT, 'maxItems': 3, 'uniqueItems': True},
              'error': {'type': 'string'}, 'page_structure': {'type': 'string'}, 'page_url': {'type': 'string'}},
             ['blocker_type', 'summary']),
    contract('lookup_answer', 'Look up the user\'s approved answer to an application question for this company and job. Use it before ask_user. '
             'When the question is a wording of a common question in My Profile, also pass common_question: an answer to any wording of it counts. '
             'Without a match, it lists related approved answers to questions worded differently. When one of them asks the same thing about the user, fill it without asking, '
             'unless the question is about this employer or role, needs a written answer, or the answer would have to change to fit.',
             {'question': _TEXT, 'common_question': _COMMON_KEY}, ['question']),
    contract('record_answer', 'Only while the user\'s auto submit mode is on: save an answer you decided yourself and filled, so later Appliers reuse it without asking. '
             'Pass question with the label copied exactly from the form and the exact answer you filled. '
             'applies_to is every_company for any fact about the user and this_company for a question about this employer or role, as for ask_user. '
             'For a common question in My Profile, pass common_question instead of applies_to. It never replaces an answer the user gave: '
             'when the user already answered the question, it returns their answer, and you fill that instead. Never use it for a contact or identity fact.',
             {'question': _TEXT, 'answer': _TEXT, 'applies_to': {'type': 'string', 'enum': sorted(ANSWER_SCOPES)}, 'common_question': _COMMON_KEY},
             ['question', 'answer']),
    contract('get_draft_feedback', 'Read the user\'s feedback on earlier drafts, newest first. Call it once per job before your first draft, '
             'and follow the general preferences it shows, such as tone, length or words to avoid. Ignore feedback about one other question only.',
             {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'description': f'How many to return, {DRAFT_FEEDBACK_LIMIT} by default.'}}),
    contract('lookup_profile', 'Look up confirmed facts from the user\'s application profile, such as contact details, work authorization, or relocation. Pass the field name, and the question text for open questions.',
             {'field': _TEXT, 'question': {'type': 'string'}}, ['field']),
    contract('list_resumes', 'List the resumes this job may use, from its frozen catalog.'),
    contract('select_resume', 'Choose one resume from list_resumes for this job, once. It copies the resume into your job folder and returns the file to upload.',
             {'resume_id': _TEXT, 'reason': _TEXT}, ['resume_id', 'reason']),
    contract('save_note', 'Save a one-line note about a widget on this application site, such as which method filled it after the normal way failed.',
             {'site': _TEXT, 'widget': _TEXT, 'text': _TEXT}, ['site', 'widget', 'text']),
    contract('get_notes', 'Read the saved notes and the tested helpers for an application site before filling its form.', {'site': _TEXT}, ['site']),
    contract('get_helper', 'Get a tested helper script for a widget. Run the returned script with javascript_tool on your tab, then read the field back.',
             {'name': _TEXT, 'args': {'type': 'object'}}, ['name', 'args']),
    contract('register_tab', 'Record the browser tab that holds this job\'s form, by the tab ID that tabs_context_mcp reports.',
             {'tab_id': {'type': 'integer', 'minimum': 1}, 'url': _TEXT}, ['tab_id', 'url']),
    contract('account_step', 'Call it on a Workday page that asks you to sign in or create an account, with the page\'s exact current URL from tabs_context_mcp. '
             'A program, not you, then creates the account or signs in on your registered tab, verifies the email itself, and returns the outcome '
             'and what to do next. It can take a few minutes. Never type an email or password on these pages yourself.',
             {'page_url': _TEXT}, ['page_url']),
    contract('fill_password', 'Call it on an employer or applicant-tracking site, other than Workday, whose create account or sign-in form shows a password field, '
             'with the page\'s exact current URL from tabs_context_mcp. A program, not you, types the user\'s shared password into every password field on the page. '
             'You never see, type or ask for a password. It refuses the sign-in pages of personal accounts such as Google, Microsoft, Apple, LinkedIn or Facebook.',
             {'page_url': _TEXT}, ['page_url']),
    contract('email_link', 'Call it when a site says it emailed the user a link to confirm or activate the account you just made, with the page\'s exact current URL. '
             'A program reads the user\'s mailbox and returns only a link on that same site. Open it in a new tab of your own tab group.',
             {'page_url': _TEXT}, ['page_url']),
    contract('email_code', 'Call it when a page asks for a verification code that the site sent to the user\'s email, before or after Submit, '
             'with the page\'s exact current URL. A program reads the user\'s mailbox and returns only the code. It can take a minute. '
             'Type the code and continue. Never use it for a code sent by text message or made by an authenticator app.',
             {'page_url': _TEXT}, ['page_url']),
    contract('report_no_sponsorship', 'Call when My Profile says the user needs visa sponsorship and the posting or the form states in plain words that the employer '
             'will not sponsor for this role, or accepts only applicants who need no sponsorship now or in the future. Pass that exact sentence. '
             'A question that only asks whether the user needs sponsorship is not such a statement. Do not ask the user. '
             'Close your tabs as it returns, then end your turn. The backend cancels the job and marks it ignored in Jobfeed.',
             {'evidence': _TEXT}, ['evidence']),
    contract('record_progress', 'Record the step you just finished.', {'step': _TEXT}, ['step']),
    contract('skip_job', 'Call only when the user\'s reply tells you to skip this posting or stop applying to it. Pass that reply\'s request_id and reply_version. '
             'The backend then cancels the job and marks it ignored in Jobfeed. Close your tabs as it returns, then end your turn.',
             {'request_id': _TEXT, 'reply_version': {'type': 'integer', 'minimum': 1}}, ['request_id', 'reply_version']),
    contract('report_closed', 'Call when the posting page itself shows the job is gone, in any wording: closed, expired, filled, removed, no longer available, '
             'no longer accepting applications, "Job not found", or a not found (404) page where the posting was. Pass that exact sentence from the page. '
             'Do not ask the user, and never report it as page_changed. Close your tabs as it returns, then end your turn. '
             'The backend marks the job unavailable, ignores it in Jobfeed and stops this session.',
             {'evidence': _TEXT}, ['evidence']),
    contract('request_submit', 'Ask whether you may click the final Submit. Call it once, right before the click. It records the attempt first.'),
    contract('record_confirmation', 'After a successful Submit, save the confirmation as evidence. First take a screenshot of the confirmation page with the computer tool and read it with get_page_text. Pass the page URL and the exact acknowledgement sentence.',
             {'source_url': _TEXT, 'acknowledgement': _TEXT}, ['source_url', 'acknowledgement']),
    contract('record_submit_outcome', 'Record what the page shows after your one Submit click: submitted, not_submitted (a visible error proves nothing was sent), or unknown.',
             {'outcome': {'type': 'string', 'enum': ['submitted', 'not_submitted', 'unknown']}, 'evidence': _TEXT}, ['outcome', 'evidence']),
    contract('record_tab_closed', 'Call it once you have closed every tab in your own tab group, only when a tool result told you to close them: after '
             'record_submit_outcome with submitted, skip_job, report_closed, or a CAPTCHA before any Submit. It records that the tabs are closed.'),
    contract('record_readback', 'Record the value you read back from a field after filling it. Pass request_id and reply_version when the value came from a user reply.',
             {'field': _TEXT, 'value': {'type': 'string'}, 'request_id': _TEXT, 'reply_version': {'type': 'integer', 'minimum': 1}},
             ['field', 'value']),
]


BROWSER_ID = 'claude-in-chrome'


SELECTED_PDF = 'selected.pdf'
_UPLOAD_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.pdf\Z')


def upload_name(materials_root):
    """The resume file name the employer sees, from resume_upload_name in the local .pipeline/applier-settings.json."""
    try:
        name = json.loads((Path(materials_root) / '.pipeline/applier-settings.json').read_text()).get('resume_upload_name', SELECTED_PDF)
    except FileNotFoundError:
        return SELECTED_PDF
    if not isinstance(name, str) or not _UPLOAD_NAME.fullmatch(name):
        raise ValueError('resume_upload_name must be a plain file name ending in .pdf')
    return name


def _common(key):
    if key not in COMMON:
        raise ValueError(f'unknown common_question {key!r}')
    return COMMON[key]


class ApplierTools:
    def __init__(self, store, job_id, owner, *, jobfeed_db=None, dashboard_url=None, evidence_dir=None, job_url=None, transcripts_home=None,
                 company=None, materials_root=None, broker=None, find_link=None, find_code=None, find_email_link=None):
        self.store, self.job_id, self.owner, self.jobfeed_db, self.dashboard_url = store, job_id, owner, jobfeed_db, dashboard_url
        self.evidence_dir, self.job_url, self.transcripts_home = evidence_dir, job_url, transcripts_home
        self.company, self.materials_root = company, Path(materials_root) if materials_root else store.root
        self.broker = broker or BrokerClient(store.root)
        self.find_link = find_link or workday_accounts.find_verification_link
        if find_code is None:
            from pipeline.email_codes import wait_for_code as find_code
        self.find_code = find_code
        if find_email_link is None:
            from pipeline.email_codes import wait_for_link as find_email_link
        self.find_email_link = find_email_link

    def ask_user(self, question, field=None, form_question=None, applies_to='this_company', draft=None, options=None, common_question=None, profile_field=None,
                 suggestions=None, stop=None):
        if self.store.skip(self.job_id) is not None:
            return {'reply': None, 'next': SKIPPED_INSTRUCTION}
        if common_question:
            item = _common(common_question)
            form_question, applies_to, options = item.question, 'every_company', list(item.options) or options
        if not (options or suggestions or draft):
            raise ValueError('The user answers with buttons, so a question needs choices. Pass options with the form\'s choices, '
                             'or suggestions with two to four answers the user is most likely to give, and ask again.')
        # Suggestions are shown like options, but a typed answer to a free-text field is as good as one of them.
        request_id = self.store.ask(self.job_id, self.owner, 'question', question, field=field, draft=draft, options=options or suggestions, company=self.company)
        result = self._reply(request_id, stop)
        if form_question and result.get('reply') is not None:
            offered = {option.strip() for option in options or ()}
            result['answer_saved'] = (self._learn(form_question, result, applies_to) if not offered or result['reply'] in offered
                                      else {'saved': False, 'reason': 'the reply is not one of the offered options'})
        if profile_field and result.get('reply') is not None:
            result['profile_saved'] = self._remember(profile_field, result['reply'])
        return result

    def _remember(self, label, value):
        """Save the reply to My Profile unless it already has the fact. A failure is reported but never stops the application."""
        if self.jobfeed_db is None:
            return {'saved': False, 'reason': 'Jobfeed is not configured for this Applier'}
        from jobfeed.my_profile_document import PersonalProfileStore
        # My Profile keeps its review record, and the lock it saves under, beside the Jobfeed database.
        store = PersonalProfileStore(self.materials_root / 'application-profile.md', Path(self.jobfeed_db).parent / 'my-profile-review.json')
        try:
            saved = store.remember(label, value)
        except (ValueError, OSError) as exc:
            return {'saved': False, 'reason': str(exc).splitlines()[0]}
        return {'saved': True} if saved else {'saved': False, 'reason': 'My Profile already has a value for this field'}

    def _learn(self, form_question, result, applies_to):
        """Save the reply to the answer bank. A failure is reported but never stops the application."""
        from pipeline.answer_bank import AnswerBank, BankError
        try:
            entry = AnswerBank(self.materials_root).learn(form_question, result['reply'], self._scope(applies_to), request_id=result['request_id'],
                                                          reply_version=result['reply_version'], channel=result['reply_source'])
        except (BankError, OSError) as exc:
            return {'saved': False, 'reason': str(exc)}
        return {'saved': True, 'entry_id': entry['id'], 'applies_to': applies_to}

    def _scope(self, applies_to):
        from pipeline.answer_bank import BankError
        scope = {'kind': ANSWER_SCOPES[applies_to]}
        if scope['kind'] == 'company':
            if not self.company:
                raise BankError('company not configured for this Applier')
            scope['company'] = self.company
        return scope

    def record_answer(self, question, answer, applies_to='this_company', common_question=None):
        """Save an answer the Applier decided itself, under the auto submit mode the user turned on, as approved for reuse."""
        if not self.store.auto_submit()['enabled']:
            raise ValueError('Auto submit mode is off, so the user approves every answer. Call ask_user instead, with a draft for a short answer.')
        if not self.company:
            raise ValueError('company not configured for this Applier')
        if common_question:
            question, applies_to = _common(common_question).question, 'every_company'
        from pipeline.answer_bank import AnswerBank, BankError
        try:
            entry = AnswerBank(self.materials_root).decide(question, answer, self._scope(applies_to), {'company': self.company, 'job_id': str(self.job_id)},
                                                           owner=self.owner, common_key=common_question)
        except BankError as exc:
            if exc.code != 'user_answer':
                raise
            return {'saved': False, 'reason': str(exc)}
        return {'saved': True, 'entry_id': entry['id'], 'applies_to': applies_to}

    def report_blocker(self, blocker_type, summary, field=None, attempts=(), error=None, page_structure=None, page_url=None, stop=None):
        if self.store.skip(self.job_id) is not None:
            return {'reply': None, 'next': SKIPPED_INSTRUCTION}
        if blocker_type == 'broken_component' and len(attempts) != 3:
            raise ValueError('a broken_component blocker needs exactly three different attempts')
        evidence = {key: value for key, value in dict(attempts=list(attempts), error=error, page_structure=page_structure, page_url=page_url).items() if value}
        if blocker_type == 'tab_missing':
            self._mark_missing()
        request_id = self.store.ask(self.job_id, self.owner, 'blocker', summary, field=field, blocker_type=blocker_type, evidence=evidence or None)
        if blocker_type == 'captcha':
            submit = self.store.submit_state(self.job_id)
            tried = submit is not None and submit['state'] not in RETRYABLE_SUBMIT
            return {'request_id': request_id, 'reply': None, 'next': CAPTCHA_INSTRUCTION if tried else CAPTCHA_REQUEUE_INSTRUCTION}
        if blocker_type == 'tab_missing' and request_id in {report['id'] for report in self.store.lost_tabs()}:
            return {'request_id': request_id, 'reply': None, 'next': TAB_LOST_INSTRUCTION}
        if self.store.request(request_id)['state'] == 'escalated':
            return {'request_id': request_id, 'reply': None, 'next': ESCALATED_INSTRUCTION}
        result = self._reply(request_id, stop)
        if result.get('reply') is not None and field:
            result['next'] = RETRY_INSTRUCTION.format(field=field)
        if blocker_type == 'submit_unknown' and result.get('reply_source') == 'email':
            result['next'] = EMAIL_CONFIRMED_INSTRUCTION
        if blocker_type == 'submit_unknown' and result.get('reply_source') == NO_EMAIL:
            result['next'] = NO_EMAIL_INSTRUCTION + CLOSE_TABS
        return result

    def _mark_missing(self):
        tabs = self.store.mark_tabs_missing(self.job_id, self.owner)
        if self.jobfeed_db is None or not tabs:
            return
        coordination = self._coordination()
        binding = coordination.bind(self.job_id, self.owner)
        coordination.observe_tabs(self.job_id, self.owner, [
            {'browser_id': BROWSER_ID, 'tab_id': t['tab_id'], 'url': t['url'], 'state': 'missing', 'observation_id': f"missing-{t['tab_id']}"} for t in tabs], binding['revision'])

    def record_tab_closed(self):
        tabs = self.store.close_tabs(self.job_id, self.owner)
        if self.jobfeed_db is not None and tabs:
            coordination = self._coordination()
            binding = coordination.bind(self.job_id, self.owner)
            coordination.observe_tabs(self.job_id, self.owner, [
                {'browser_id': BROWSER_ID, 'tab_id': t['tab_id'], 'url': t['url'], 'state': 'closed', 'observation_id': f"closed-{t['tab_id']}"} for t in tabs], binding['revision'])
        submitted = (self.store.submit_state(self.job_id) or {}).get('state') == 'submitted'
        return {'closed': [t['tab_id'] for t in tabs],
                'next': 'Call record_progress with "submitted" and end with a one-line summary.' if submitted else 'End your turn now.'}

    def _reply(self, request_id, stop):
        reply = self.store.wait_for_reply(request_id, stop=stop)
        if reply is None and self.store.request(request_id)['state'] == 'withdrawn':
            return {'request_id': request_id, 'reply': None, 'next': SKIPPED_INSTRUCTION}
        if reply is None:
            return {'request_id': request_id, 'reply': None, 'next': 'The wait was cancelled. Call the same tool again to keep waiting.'}
        if reply['intent'] == 'change_request':
            return {'request_id': request_id, 'feedback': reply['text'], 'draft': self.store.request(request_id)['draft'], 'next': REDRAFT_INSTRUCTION}
        return {'request_id': request_id, 'reply_version': reply['version'], 'reply': reply['text'], 'reply_source': reply['source_kind']}

    def lookup_answer(self, question, common_question=None):
        if common_question:
            _common(common_question)
        if not self.company:
            return {'allowed': False, 'reason': 'company not configured for this Applier'}
        from pipeline.answer_bank import AnswerBank
        try:
            bank, context = AnswerBank(self.materials_root), {'company': self.company, 'job_id': str(self.job_id)}
            result = bank.resolve(question, context, common_key=common_question)
            if not result['allowed'] and (related := bank.related(question, context)):
                result['related'] = related
            return result
        except (OSError, ValueError) as exc:
            return {'allowed': False, 'reason': f'answer bank unavailable: {exc}'}

    def get_draft_feedback(self, limit=DRAFT_FEEDBACK_LIMIT):
        return {'feedback': self.store.draft_feedback(limit)}

    def lookup_profile(self, field, question=''):
        from types import SimpleNamespace
        from pipeline.profile_lookup import compact_material, job_material
        job = SimpleNamespace(id=self.job_id, company=self.company or '')
        result = job_material(self.materials_root, job, questions=[{'text': question}] if question else (), specification={'aliases': [field]})
        return compact_material(result)

    def _catalog(self):
        if self.evidence_dir is None:
            raise ValueError('this Applier has no job folder configured')
        path = Path(self.evidence_dir) / 'resume-catalog.json'
        if not path.exists():
            raise ValueError('this job has no frozen resume catalog; use the resume in your materials')
        return json.loads(path.read_text())

    def list_resumes(self):
        contract = self._catalog()
        return {'resumes': [{'resume_id': e['id'], 'label': e.get('label', e['id'])} for e in contract['catalog']['entries']]}

    def select_resume(self, resume_id, reason):
        from dataclasses import replace
        from pipeline.config import PipelineConfig
        from pipeline.resume_catalog import select_resume
        contract = self._catalog()
        config = replace(PipelineConfig.discover(self.materials_root), applications_root=Path(self.evidence_dir).resolve().parent,
                         **({'jobfeed_db': Path(self.jobfeed_db)} if self.jobfeed_db else {}))
        selected = select_resume(config, contract['scope'], self.job_id, self.owner, resume_id, reason, binding_wait=0)
        name = upload_name(self.materials_root)
        upload = Path(self.evidence_dir).resolve() / name
        if name != SELECTED_PDF:
            # The employer sees this name. selected.pdf stays the verified original; this is a byte-identical copy.
            upload.write_bytes(Path(selected['paths']['pdf']).read_bytes())
        self.store.record_resume(self.job_id, self.owner, selected['resume_id'], selected['label'], name)
        return {'resume_id': selected['resume_id'], 'label': selected['label'], 'upload': str(upload),
                'next': 'Upload this exact file to the resume field with file_upload, then read the field back.'}

    def save_note(self, site, widget, text):
        return {'note_id': self.store.note(self.job_id, self.owner, site, widget, text)}

    def get_notes(self, site):
        return {'notes': self.store.notes(site), 'helpers': applier_helpers.for_site(site)}

    def get_helper(self, name, args):
        return {'script': applier_helpers.script(name, args), 'next': 'Run this script with javascript_tool on your tab. It returns ok and the value it read back.'}

    def _coordination(self):
        scope = next(job['scope'] for job in self.store.jobs() if job['job_id'] == self.job_id)
        return CoordinationStore(self.store.root, scope=scope, jobfeed_db=self.jobfeed_db)

    def register_tab(self, tab_id, url):
        self.store.register_tab(self.job_id, self.owner, tab_id, url)
        if self.jobfeed_db is None:
            return {'recorded': True}
        # The Dashboard binds Focus to the pipeline's coordination records, not to this store.
        coordination = self._coordination()
        binding = coordination.bind(self.job_id, self.owner)
        try:
            coordination.observe_tabs(self.job_id, self.owner, [{'browser_id': BROWSER_ID, 'tab_id': str(tab_id), 'url': url}], binding['revision'])
        except ValueError as exc:
            if 'another job' in str(exc):
                self.store.alert(self.job_id, 'owner_conflict', f'tab {tab_id} belongs to another job')
                raise ValueError(f'tab {tab_id} belongs to another job. Do not use it. Call report_blocker with blocker_type foreign_owner.') from None
            raise
        return {'recorded': True, 'coordination': True, 'dashboard_focus': self.connect_focus(tab_id, url)}

    def connect_focus(self, tab_id, url):
        """Link the tab to the Dashboard's Open application form button. A failure never blocks the form."""
        if self.dashboard_url is None:
            return 'not configured'
        try:
            bridge = ProfileBridge(self.dashboard_url)
            proof = bridge.probe([{'native_tab_id': tab_id, 'url': url}])
            binding = bridge.connect(self.job_id, self.owner, proof['profile_proof'], {'native_tab_id': tab_id, 'url': url})
            return binding.get('state', 'unknown')
        except RerunError as exc:
            return f'unavailable: {exc}'

    def record_progress(self, step):
        self.store.progress(self.job_id, self.owner, step)
        return {'recorded': True}

    def account_step(self, page_url):
        """Have the account program sign in on the job's own Workday tab. The model never sees the password."""
        site = workday_accounts.workday_site(page_url)
        if site is None:
            raise ValueError('account_step works only on Workday pages (myworkdayjobs.com or myworkdaysite.com)')
        tabs = self.store._read("SELECT url FROM applier_tabs WHERE job_id=? AND state='observed'", (self.job_id,))
        if not any(workday_accounts.workday_site(tab['url']) == site for tab in tabs):
            raise ValueError('page_url must be on the same Workday site as your registered tab. Call register_tab first.')
        self.store.progress(self.job_id, self.owner, f'account step on {site.key}')
        email = self._profile_email()
        result = workday_accounts.account_step(self.broker, workday_accounts.AccountStore(self.store.root), site, email, self.job_id, page_url, self.find_link)
        if result['outcome'] in workday_accounts.ALERTS:
            self.store.alert(self.job_id, 'account_program', ACCOUNT_ALERTS[result['outcome']])
        return result

    def _profile_email(self):
        from jobfeed.my_profile_document import PersonalProfileStore
        state = PersonalProfileStore(self.materials_root / 'application-profile.md', self.materials_root / '.pipeline' / 'my-profile-review.json').state()
        if not state.personal_details.primary_email:
            raise ValueError('My Profile has no Primary email, so no account can be made. Call report_blocker with blocker_type missing_fact.')
        return state.personal_details.primary_email

    def email_code(self, page_url):
        """Read the verification code the site emailed the user. Only the digits reach the model."""
        from pipeline import email_codes
        tied_to = email_codes.names(self.company, page_url)
        if not tied_to:
            raise ValueError('page_url must be the exact URL of the page that asks for the code')
        self.store.progress(self.job_id, self.owner, 'email code')
        code = self.find_code(self._profile_email(), tied_to)
        return {'code': code, 'next': CODE_FOUND if code else CODE_MISSING}

    def fill_password(self, page_url):
        """Have the account program type the shared password into the page's password fields. The model never sees it."""
        if not self.store._read("SELECT 1 FROM applier_tabs WHERE job_id=? AND state='observed'", (self.job_id,)):
            raise ValueError('call register_tab first')
        self.store.progress(self.job_id, self.owner, f'account password on {urlsplit(page_url).hostname}')
        result = self.broker.fill_password(self.job_id, page_url)
        outcome = result.get('outcome')
        text = PASSWORD_NEXT.get(outcome, PASSWORD_FAILED)
        return {'outcome': outcome, 'next': text.format(fields=result.get('fields'), reason=result.get('reason') or 'no reason shown')}

    def email_link(self, page_url):
        """Read the account confirmation link the site emailed the user. Only a link on the page's own site reaches the model."""
        from pipeline import email_codes
        site = email_codes.site_of(urlsplit(page_url).hostname)
        if urlsplit(page_url).scheme != 'https' or '.' not in site:
            raise ValueError('page_url must be the exact https URL of the page that asks you to confirm the account')
        self.store.progress(self.job_id, self.owner, 'email link')
        link = self.find_email_link(self._profile_email(), site)
        return {'link': link, 'next': LINK_FOUND if link else LINK_MISSING}

    def report_no_sponsorship(self, evidence):
        self.store.record_no_sponsorship(self.job_id, self.owner, evidence)
        return {'recorded': True, 'next': NO_SPONSORSHIP_INSTRUCTION + CLOSE_TABS}

    def skip_job(self, request_id, reply_version):
        self.store.record_skip(self.job_id, self.owner, request_id, reply_version)
        return {'skipped': True, 'next': CLOSE_TABS}

    def report_closed(self, evidence):
        self.store.record_closed(self.job_id, self.owner, evidence)
        return {'recorded': True, 'next': CLOSE_TABS}

    def request_submit(self):
        allowed, reason = self.store.request_submit(self.job_id, self.owner)
        if allowed:
            return {'authorized': True, 'next': 'Click the final Submit exactly once now. Then read the page and call record_submit_outcome.'}
        if reason == 'skipped':
            return {'authorized': False, 'next': SKIPPED_INSTRUCTION}
        if reason == 'not_authorized':
            return {'authorized': False, 'next': 'Do not submit. Call record_progress with "ready for review" and end with a one-line summary.'}
        return {'authorized': False, 'next': f'Submit was already attempted ({reason.removeprefix("already_")}). Never click it again. '
                                            'If the outcome is unclear, call report_blocker with blocker_type submit_unknown.'}

    def record_submit_outcome(self, outcome, evidence):
        if outcome == 'submitted' and self.evidence_dir is not None:
            # A submitted outcome must rest on a receipt that Jobfeed itself accepts.
            from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation
            try:
                read_confirmation(Path(self.evidence_dir), self.job_id, self.job_url, company=self.company)
            except (EvidenceValidationError, OSError, ValueError) as exc:
                raise ValueError(f'no valid confirmation saved yet ({exc}); call record_confirmation first, or record unknown') from None
        self.store.record_submit_outcome(self.job_id, self.owner, outcome, evidence)
        nxt = {'submitted': CLOSE_TABS,
               'not_submitted': 'Fix what the page reported, read the fields back, then call request_submit again.',
               'unknown': 'Never click Submit again. Look for a confirmation on the page without changing it. If you find one, save it with record_confirmation, '
                          'then call record_submit_outcome with submitted. Otherwise call report_blocker with blocker_type submit_unknown and your evidence.'}
        return {'recorded': outcome, 'next': nxt[outcome]}

    def record_confirmation(self, source_url, acknowledgement):
        if self.evidence_dir is None or self.job_url is None:
            raise ValueError('this Applier has no evidence folder configured')
        submit = self.store.submit_state(self.job_id)
        if submit is None or submit['state'] not in {'requested', 'unknown', 'submitted'}:
            raise ValueError('save a confirmation only after an authorized Submit')
        launch = self.store._read('SELECT session_id,config FROM applier_launches WHERE launch_id=?', (self.owner,))
        if not launch or not launch[0]['session_id']:
            raise ValueError('the session transcript for this Applier is not known yet')
        cwd = json.loads(launch[0]['config'])['cwd']
        tabs = self.store._read("SELECT tab_id,url FROM applier_tabs WHERE job_id=? AND state='observed'", (self.job_id,))
        if len(tabs) > 1:
            # A job board opens the employer's form in a second registered tab; the confirmation shows on the form's site.
            host = urlsplit(source_url).hostname
            tabs = [tab for tab in tabs if urlsplit(tab['url']).hostname == host]
        if len(tabs) != 1:
            raise ValueError('evidence needs exactly one registered form tab for this job on the confirmation page\'s site')
        transcript = claude_evidence.transcript_path(cwd, launch[0]['session_id'], self.transcripts_home)
        shot, page = claude_evidence.latest_capture(transcript, submit['requested_at'], tab_id=int(tabs[0]['tab_id']))
        saved = claude_evidence.write_receipt(self.evidence_dir, job_id=self.job_id, job_url=self.job_url, source_url=source_url,
                                              acknowledgement=acknowledgement, shot=shot, page=page)
        return {'saved': saved, 'next': 'Call record_submit_outcome with submitted if you have not yet.'}

    def record_readback(self, field, value, request_id=None, reply_version=None):
        return {'readback_id': self.store.readback(self.job_id, self.owner, field, value, request_id=request_id, reply_version=reply_version)}


class StdioServer:
    """Runs each tool call on its own thread, so a waiting ask_user never blocks ping or cancellation."""

    WAITING = frozenset({'ask_user', 'report_blocker'})

    def __init__(self, tools):
        self.tools, self.initialized = tools, False
        self.lock, self.calls = threading.Lock(), {}

    def response(self, request, stdout=None):
        request_id = request.get('id') if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                raise ValueError('invalid JSON-RPC request')
            params = request.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            method = request['method']
            if 'id' not in request:
                if method == 'notifications/cancelled' and (stop := self.calls.get(params.get('requestId'))) is not None:
                    stop.set()
                return None
            if type(request_id) not in (str, int):
                raise ValueError('request id must be string or integer')
            if method == 'initialize':
                protocol = params.get('protocolVersion')
                if protocol not in {'2024-11-05', '2025-03-26', '2025-06-18'}:
                    protocol = '2024-11-05'
                self.initialized = True
                value = dict(protocolVersion=protocol, capabilities={'tools': {}}, serverInfo={'name': 'applier', 'version': '1.0.0'})
            elif method == 'ping':
                value = {}
            elif not self.initialized:
                raise ValueError('initialize before using tools')
            elif method == 'tools/list':
                value = {'tools': TOOLS}
            elif method == 'tools/call':
                if set(params) - {'name', 'arguments', '_meta'}:
                    raise ValueError('invalid tool call')
                name, arguments = params.get('name'), params.get('arguments', {})
                schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
                if schema is None or not isinstance(arguments, dict) or set(arguments) - set(schema['properties']) or not set(schema['required']) <= set(arguments):
                    raise ValueError('tool arguments do not match the fixed Applier contract')
                if name in self.WAITING:
                    self.calls[request_id] = threading.Event()
                if stdout is None:
                    return self._call(request_id, name, arguments)
                threading.Thread(target=self._send, args=(stdout, self._call, request_id, name, arguments), daemon=True).start()
                return None
            else:
                return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'method not found'}}
            return {'jsonrpc': '2.0', 'id': request_id, 'result': value}
        except (ValueError, TypeError) as exc:
            return {'jsonrpc': '2.0', 'id': request_id if type(request_id) in (str, int) else None, 'error': {'code': -32602, 'message': str(exc)}}

    def _call(self, request_id, name, arguments):
        if name in self.WAITING:
            arguments = dict(arguments, stop=self.calls[request_id])
        try:
            result = getattr(self.tools, name)(**arguments)
            value = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': False}
        except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError) as exc:
            value = {'content': [{'type': 'text', 'text': json.dumps({'error': str(exc)})}], 'isError': True}
        finally:
            self.calls.pop(request_id, None)
        return {'jsonrpc': '2.0', 'id': request_id, 'result': value}

    def _send(self, stdout, produce, *args):
        self._write(stdout, produce(*args))

    def _write(self, stdout, response):
        with self.lock:
            stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            stdout.flush()

    def serve(self, stdin, stdout):
        while True:
            line = stdin.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                while line and not line.endswith(b'\n'):
                    line = stdin.readline(MAX_LINE + 1)
                response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'request line exceeds limit'}}
            else:
                try:
                    response = self.response(json.loads(line), stdout)
                except (ValueError, UnicodeError):
                    response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'invalid JSON'}}
            if response is not None:
                self._write(stdout, response)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--job', type=int, required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--jobfeed-db', type=Path)
    parser.add_argument('--dashboard-url')
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--job-url')
    parser.add_argument('--company')
    parser.add_argument('--materials-root', type=Path)
    args = parser.parse_args(argv)
    tools = ApplierTools(ApplierRuntime(args.root), args.job, args.owner, jobfeed_db=args.jobfeed_db, dashboard_url=args.dashboard_url,
                         evidence_dir=args.evidence_dir, job_url=args.job_url, company=args.company, materials_root=args.materials_root)
    StdioServer(tools).serve(sys.stdin.buffer, sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
