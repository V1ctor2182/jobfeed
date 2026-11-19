import io
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from pipeline.answer_bank import AnswerBank
from pipeline.applier_mcp import TOOLS, ApplierTools, StdioServer, upload_name
from pipeline.applier_runtime import ApplierRuntime

PIPELINE = Path(__file__).resolve().parents[1]
LIKELY = ['None', 'N/A']


def call(server, name, arguments, request_id=1):
    response = server.response({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
    return response['result']['isError'], json.loads(response['result']['content'][0]['text'])


class ApplierMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.store.bind(1, 'owner-1', 'batch')
        self.server = StdioServer(ApplierTools(self.store, 1, 'owner-1'))
        self.server.response({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})

    def test_tools_must_match_the_fixed_contract(self):
        response = self.server.response({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'record_progress', 'arguments': {'step': 'x', 'job_id': 2}}})
        self.assertIn('fixed Applier contract', response['error']['message'])
        response = self.server.response({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'reply', 'arguments': {}}})
        self.assertIn('fixed Applier contract', response['error']['message'])

    def test_the_resume_is_uploaded_under_the_configured_name_and_remembered_for_the_summary(self):
        folder = self.root / 'applications' / 'job-1'
        folder.mkdir(parents=True)
        (folder / 'resume-catalog.json').write_text(json.dumps({'scope': 'batch'}))
        (folder / 'selected.pdf').write_bytes(b'%PDF-chosen')
        tools = ApplierTools(self.store, 1, 'owner-1', evidence_dir=folder, materials_root=self.root)
        chosen = {'resume_id': 'swe', 'label': 'Software Engineer', 'paths': {'pdf': str(folder / 'selected.pdf')}}
        with patch('pipeline.resume_catalog.select_resume', return_value=chosen):
            self.assertEqual(tools.select_resume('swe', 'matches the role')['upload'], str(folder.resolve() / 'selected.pdf'))
            (self.root / '.pipeline/applier-settings.json').write_text(json.dumps({'resume_upload_name': 'resume-alexdoe.pdf'}))
            result = tools.select_resume('swe', 'matches the role')
        self.assertEqual(result['upload'], str(folder.resolve() / 'resume-alexdoe.pdf'))
        self.assertEqual(Path(result['upload']).read_bytes(), b'%PDF-chosen')
        self.assertEqual(self.store.resume(1), {'resume_id': 'swe', 'label': 'Software Engineer', 'upload_name': 'resume-alexdoe.pdf'})
        (self.root / '.pipeline/applier-settings.json').write_text(json.dumps({'resume_upload_name': '../resume.pdf'}))
        with self.assertRaisesRegex(ValueError, 'plain file name'):
            upload_name(self.root)

    def test_broken_component_needs_three_attempts(self):
        failed, result = call(self.server, 'report_blocker', {'blocker_type': 'broken_component', 'summary': 'Location picker is stuck', 'field': 'location', 'attempts': ['form_input', 'computer']})
        self.assertTrue(failed)
        self.assertIn('exactly three', result['error'])

    def test_on_the_strong_model_a_blocker_reply_comes_back_with_a_retry_instruction(self):
        def answer():
            while not (requests := self._open_requests()):
                time.sleep(0.02)
            self.store.reply(requests[0], 'done', source_kind='discord', source_id='msg-1', sender_id='user-1')
        threading.Thread(target=answer, daemon=True).start()
        self.store.bind(2, 'owner-2', 'batch', 'opus')
        strong = StdioServer(ApplierTools(self.store, 2, 'owner-2'))
        strong.response({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        failed, result = call(strong, 'report_blocker', {'blocker_type': 'broken_component', 'summary': 'Location picker is stuck', 'field': 'location',
                                                              'attempts': ['form_input', 'computer', 'javascript_tool']})
        self.assertFalse(failed)
        self.assertEqual((result['reply'], result['reply_version']), ('done', 1))
        self.assertIn('retry the field "location"', result['next'])

    def test_a_cancelled_wait_returns_and_can_be_called_again(self):
        out = io.StringIO()
        self.server.response({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'ask_user', 'arguments': {'question': 'Start date?', 'suggestions': ['June 1', 'July 1']}}}, out)
        while 7 not in self.server.calls:
            time.sleep(0.01)
        self.server.response({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 7}})
        deadline = time.monotonic() + 5
        while not out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        result = json.loads(json.loads(out.getvalue())['result']['content'][0]['text'])
        self.assertIsNone(result['reply'])
        self.assertIn('call the same tool again', result['next'].lower())

    def test_stdio_answers_ping_while_ask_user_waits(self):
        env = dict(os.environ, PYTHONPATH=str(PIPELINE))
        process = subprocess.Popen([sys.executable, '-m', 'pipeline.applier_mcp', '--root', str(self.root), '--job', '1', '--owner', 'owner-1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=PIPELINE, env=env, text=True)
        self.addCleanup(process.kill)

        def send(message):
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()

        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(json.loads(process.stdout.readline())['id'], 1)
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'ask_user', 'arguments': {'question': 'Start date?', 'suggestions': ['June 1', 'July 1']}}})
        send({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
        self.assertEqual(json.loads(process.stdout.readline()), {'jsonrpc': '2.0', 'id': 3, 'result': {}})
        while not (requests := self._open_requests()):
            time.sleep(0.02)
        self.store.reply(requests[0], 'June 1', source_kind='discord', source_id='msg-9', sender_id='user-1')
        answer = json.loads(process.stdout.readline())
        self.assertEqual(answer['id'], 2)
        self.assertEqual(json.loads(answer['result']['content'][0]['text'])['reply'], 'June 1')

    def test_register_tab_records_the_coordination_tab_the_dashboard_binds(self):
        jobfeed = self.root / 'jobfeed.sqlite3'
        with sqlite3.connect(jobfeed) as db:
            db.execute('CREATE TABLE application_queue(job_id INTEGER PRIMARY KEY,codex_application_session_id TEXT)')
            db.execute("INSERT INTO application_queue VALUES(1,'owner-1')")
        tools = ApplierTools(self.store, 1, 'owner-1', jobfeed_db=jobfeed)
        self.assertEqual(tools.register_tab(1778002301, 'http://127.0.0.1:8790/job1.html'), {'recorded': True, 'coordination': True, 'dashboard_focus': 'not configured'})
        tools.register_tab(1778002301, 'http://127.0.0.1:8790/job1.html')
        with sqlite3.connect(self.root / '.pipeline' / 'coordination.sqlite3') as db:
            rows = db.execute('SELECT scope,job_id,owner,browser_id,tab_id,origin,state FROM coordination_tabs').fetchall()
        self.assertEqual(rows, [('batch', 1, 'owner-1', 'claude-in-chrome', '1778002301', 'http://127.0.0.1:8790', 'observed')])

    def test_notes_list_site_helpers_and_get_helper_returns_a_runnable_script(self):
        failed, notes = call(self.server, 'get_notes', {'site': 'acme.wd1.myworkdayjobs.com'})
        self.assertFalse(failed)
        self.assertIn('searchable-dropdown-pick', [h['name'] for h in notes['helpers']])
        failed, helper = call(self.server, 'get_helper', {'name': 'searchable-dropdown-pick', 'args': {'input': '#c', 'option': 'Canada'}})
        self.assertFalse(failed)
        self.assertTrue(helper['script'].endswith('({"input": "#c", "option": "Canada"})'))
        failed, result = call(self.server, 'get_helper', {'name': 'nope', 'args': {}})
        self.assertTrue(failed)

    def _jobfeed(self, owners):
        jobfeed = self.root / 'jobfeed.sqlite3'
        with sqlite3.connect(jobfeed) as db:
            db.execute('CREATE TABLE IF NOT EXISTS application_queue(job_id INTEGER PRIMARY KEY,codex_application_session_id TEXT)')
            db.executemany('INSERT OR REPLACE INTO application_queue VALUES(?,?)', owners)
        return jobfeed

    def test_a_missing_tab_is_marked_in_both_stores_and_blocks_a_quiet_reopen(self):
        jobfeed = self._jobfeed([(1, 'owner-1')])
        tools = ApplierTools(self.store, 1, 'owner-1', jobfeed_db=jobfeed)
        tools.register_tab(41, 'http://127.0.0.1:8790/job1.html')
        out = io.StringIO()
        self.server.tools = tools
        self.server.response({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {'name': 'report_blocker', 'arguments': {'blocker_type': 'tab_missing', 'summary': 'My form tab is gone'}}}, out)
        while not self._open_requests():
            time.sleep(0.02)
        with sqlite3.connect(self.root / '.pipeline' / 'coordination.sqlite3') as db:
            self.assertEqual(db.execute('SELECT tab_id,state FROM coordination_tabs').fetchall(), [('41', 'missing')])
        with self.assertRaisesRegex(ValueError, 'original form tab is missing'):
            tools.register_tab(42, 'http://127.0.0.1:8790/job1.html')
        self.store.reply(self._open_requests()[0], 'reopen', source_kind='discord', source_id='msg-r', sender_id='user-1')
        deadline = time.monotonic() + 5
        while not out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(tools.register_tab(42, 'http://127.0.0.1:8790/job1.html')['recorded'], True)

    def test_a_tab_owned_by_another_job_raises_a_conflict_alert(self):
        jobfeed = self._jobfeed([(1, 'owner-1'), (2, 'owner-2')])
        self.store.bind(2, 'owner-2', 'batch')
        ApplierTools(self.store, 2, 'owner-2', jobfeed_db=jobfeed).register_tab(77, 'http://127.0.0.1:8790/job2.html')
        with self.assertRaisesRegex(ValueError, 'foreign_owner'):
            ApplierTools(self.store, 1, 'owner-1', jobfeed_db=jobfeed).register_tab(77, 'http://127.0.0.1:8790/job2.html')
        self.assertEqual([(a['job_id'], a['kind']) for a in self.store.alerts()], [(1, 'owner_conflict')])

    def test_lookup_answer_returns_only_approved_answers_for_this_company_and_job(self):
        from pipeline.answer_bank import AnswerBank
        bank = AnswerBank(self.root)
        bank.pending('Why us?', 'Because of the mission.', {'company': 'Rilla', 'job_id': '1'})
        tools = ApplierTools(self.store, 1, 'owner-1', company='Rilla')
        self.assertFalse(tools.lookup_answer('Why us?')['allowed'])
        store = bank.read()
        bank.mutate({'action': 'approve', 'entry_id': store['entries'][0]['id'], 'expected_version': 1, 'expected_revision': store['revision']}, dashboard=True)
        self.assertEqual(tools.lookup_answer('Why us?')['answer'], 'Because of the mission.')
        self.assertFalse(ApplierTools(self.store, 1, 'owner-1', company='Other Co').lookup_answer('Why us?')['allowed'])
        self.assertIn('unavailable', ApplierTools(ApplierRuntime(self.root / 'empty'), 1, 'owner-1', company='Rilla').lookup_answer('Why us?')['reason'])

    def test_lookup_answer_finds_common_answers_saved_in_the_profile_under_other_wordings(self):
        from pipeline.answer_bank import AnswerBank
        bank = AnswerBank(self.root)
        for question, answer in (('What is your race or ethnicity?', 'Asian'), ('Are you at least 18 years old?', 'Yes'),
                                 ('What is your veteran status?', 'I am not a protected veteran')):
            bank.mutate({'action': 'save', 'expected_revision': bank.read()['revision'], 'draft': {'question': question, 'answer': answer, 'scope': {'kind': 'global'}}}, dashboard=True)
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme')
        for wording, answer in (('Please identify your race', 'Asian'), ('Ethnicity', 'Asian'), ('Are you over 18?', 'Yes'),
                                ('Are you 18 years of age or older? *', 'Yes'), ('Protected Veteran Status', 'I am not a protected veteran')):
            self.assertEqual(tools.lookup_answer(wording)['answer'], answer, wording)
        self.assertFalse(tools.lookup_answer('Do you have a disability?')['allowed'])

    def test_a_common_question_in_employer_wording_finds_the_my_profile_answer_by_its_key(self):
        from pipeline.answer_bank import AnswerBank
        bank = AnswerBank(self.root)
        bank.mutate({'action': 'save', 'expected_revision': bank.read()['revision'],
                     'draft': {'question': 'What is your gender?', 'answer': 'Male', 'scope': {'kind': 'global'}}}, dashboard=True)
        server = StdioServer(ApplierTools(self.store, 1, 'owner-1', company='Acme'))
        server.response({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        wording = 'Please select the appropriate gender with which you identify from the drop down box below.'
        self.assertFalse(call(server, 'lookup_answer', {'question': wording})[1]['allowed'])
        self.assertEqual(call(server, 'lookup_answer', {'question': wording, 'common_question': 'gender'})[1]['answer'], 'Male')
        self.assertTrue(call(server, 'lookup_answer', {'question': wording, 'common_question': 'nickname'})[0])

    def test_a_common_question_offers_my_profile_choices_and_saves_the_reply_to_my_profile(self):
        from pipeline.answer_bank import AnswerBank, common_questions
        from pipeline.common_questions import CATALOG
        race = next(item for item in CATALOG if item.key == 'race')
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme')
        self.answer_next_question('1')
        result = tools.ask_user('How should I answer race?', field='Race', common_question='race', options=['A', 'B'])
        self.assertEqual(self.store.request(result['request_id'])['options'], list(race.options))
        self.assertEqual((result['reply'], result['answer_saved']['saved']), ('1', False))
        self.answer_next_question('Asian')
        self.assertTrue(tools.ask_user('Which race should I select?', field='Race', common_question='race')['answer_saved']['saved'])
        self.answer_next_question('he/him')
        self.assertTrue(tools.ask_user('Your pronouns?', field='Pronouns', common_question='pronouns', suggestions=['he/him', 'they/them'])['answer_saved']['saved'])
        store = AnswerBank(self.root).read()
        answers = {e['id']: e['versions'][-1]['answer'] for e in store['entries']}
        profile = {item['key']: answers.get(item['entry_id']) for item in common_questions(store)}
        self.assertEqual((profile['race'], profile['pronouns']), ('Asian', 'he/him'))
        self.store.bind(2, 'owner-2', 'batch')
        self.assertEqual(ApplierTools(self.store, 2, 'owner-2', company='Other Co').lookup_answer('Race/Ethnicity')['answer'], 'Asian')

    def test_a_saved_relocation_answer_covers_an_employer_specific_relocation_question(self):
        from pipeline.answer_bank import AnswerBank
        AnswerBank(self.root).learn('are you willing to relocate?', 'Yes', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        tools = ApplierTools(self.store, 1, 'owner-1', company='Notion')
        result = tools.lookup_answer('This role requires that you are willing to relocate to San Francisco, CA. Are you willing?', common_question='relocation')
        self.assertEqual((result['allowed'], result['answer']), (True, 'Yes'))

    def test_an_onsite_reply_at_one_company_answers_the_next_company(self):
        self.answer_next_question('Yes')
        result = ApplierTools(self.store, 1, 'owner-1', company='Netic').ask_user(
            'Netic asks whether you can work from its SF office five days a week. Can you?', field='Onsite', common_question='onsite')
        self.assertEqual((result['answer_saved']['saved'], result['answer_saved']['applies_to']), (True, 'every_company'))
        self.store.bind(2, 'owner-2', 'batch')
        later = ApplierTools(self.store, 2, 'owner-2', company='Chalk').lookup_answer('If US based, are you OK with working in office 5 days/ week?')
        self.assertEqual(later['answer'], 'Yes')

    def test_a_company_answer_to_the_exact_question_beats_the_common_answer_there(self):
        from pipeline.answer_bank import AnswerBank
        question = 'Are you able to work from our SF office five days per week?'
        bank = AnswerBank(self.root)
        bank.learn('Are you willing to work onsite in the office?', 'Yes', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        bank.learn(question, 'No', {'kind': 'company', 'company': 'Netic'}, request_id='r2', reply_version=1, channel='discord')
        self.assertEqual(ApplierTools(self.store, 1, 'owner-1', company='Netic').lookup_answer(question, common_question='onsite')['answer'], 'No')

    def test_only_a_reply_that_is_one_of_the_offered_options_is_saved(self):
        tools = ApplierTools(self.store, 1, 'owner-1', company='Visa')
        question = 'Will you now or in the future require sponsorship?'
        self.answer_next_question('1')
        result = tools.ask_user('Sponsorship? (1) Yes (2) No', form_question=question, applies_to='every_company', options=['Yes', 'No'])
        self.assertEqual((result['reply'], result['answer_saved']['saved']), ('1', False))
        self.assertFalse(tools.lookup_answer(question)['allowed'])
        self.answer_next_question('No')
        self.assertTrue(tools.ask_user('Sponsorship?', form_question=question, applies_to='every_company', options=['Yes', 'No'])['answer_saved']['saved'])
        self.assertEqual(tools.lookup_answer(question)['answer'], 'No')

    def test_a_reply_about_a_contact_fact_is_never_saved_as_an_answer(self):
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme')
        for label, reply in (('Email', 'gmail'), ('Email Address (required)', 'ye s'), ('Phone Number*', '555-0100'), ('LinkedIn URL', 'yes')):
            self.answer_next_question(reply)
            result = tools.ask_user(f'Which value for {label}?', field=label, form_question=label, applies_to='every_company', suggestions=LIKELY)
            self.assertEqual((result['reply'], result['answer_saved']['saved']), (reply, False), label)
            self.assertFalse(tools.lookup_answer(label)['allowed'], label)

    def test_a_link_the_profile_lacks_is_saved_to_my_profile_so_the_next_applier_finds_it(self):
        profile = self.root / 'application-profile.md'
        profile.write_text('# Application profile\n\n## Identity and contact\n- Portfolio: https://example.test\n\nNotes stay.\n')
        jobfeed_db = self.root / 'jobfeed/data/jobfeed.sqlite'
        tools = ApplierTools(self.store, 1, 'owner-1', company='Acme', jobfeed_db=jobfeed_db, materials_root=self.root)
        self.answer_next_question('skip')
        self.assertFalse(tools.ask_user('Your GitHub URL?', field='GitHub', profile_field='github_url', suggestions=LIKELY)['profile_saved']['saved'])
        self.assertNotIn('GitHub', profile.read_text())
        self.answer_next_question('https://github.com/example')
        self.assertTrue(tools.ask_user('Which GitHub profile URL should I enter?', field='GitHub', profile_field='github_url', suggestions=LIKELY)['profile_saved']['saved'])
        self.answer_next_question('https://other.test')
        self.assertFalse(tools.ask_user('Your portfolio?', field='Portfolio', profile_field='portfolio_url', suggestions=LIKELY)['profile_saved']['saved'])
        text = profile.read_text()
        self.assertIn('- GitHub: https://github.com/example\n', text)
        self.assertIn('- Portfolio: https://example.test\n', text)
        self.assertIn('Notes stay.', text)
        self.assertFalse((jobfeed_db.parent / 'my-profile-review.json').exists())
        self.store.bind(2, 'owner-2', 'batch')
        found = ApplierTools(self.store, 2, 'owner-2', company='Other Co', materials_root=self.root).lookup_profile('github')
        self.assertIn('https://github.com/example', json.dumps(found))

    def profile_tools(self, text):
        profile = self.root / 'application-profile.md'
        profile.write_text(text)
        return profile, ApplierTools(self.store, 1, 'owner-1', company='Acme', jobfeed_db=self.root / 'jobfeed/data/jobfeed.sqlite', materials_root=self.root)

    def ask_profile(self, tools, label, reply):
        self.answer_next_question(reply)
        return tools.ask_user(f'Your {label}?', field=label, profile_field=label, suggestions=LIKELY)['profile_saved']

    def test_a_full_name_reply_is_saved_so_the_next_applier_finds_it_instead_of_asking(self):
        profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Legal name: Alexandra Rivera\n- Preferred name: Alex\n')
        self.assertEqual(self.ask_profile(tools, 'Full name', 'Alex Rivera'), {'saved': True})
        self.store.bind(2, 'owner-2', 'batch')
        found = ApplierTools(self.store, 2, 'owner-2', company='Other Co', materials_root=self.root).lookup_profile('Full Name')
        self.assertIn('Full name: Alex Rivera', json.dumps(found))
        self.assertIn('- Legal name: Alexandra Rivera\n', profile.read_text())

    def test_a_reply_that_is_not_a_value_is_never_saved(self):
        profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Legal name: Alexandra Rivera\n')
        before = profile.read_bytes()
        for label, reply in (('Full name', 'ye s'), ('Full name', 'skip'), ('Pronouns', 'none'), ('Phone', 'ye s'), ('GitHub', 'skip'), ('Email', 'gmail')):
            saved = self.ask_profile(tools, label, reply)
            self.assertFalse(saved['saved'], (label, reply))
            self.assertTrue(saved['reason'], (label, reply))
        self.assertEqual(profile.read_bytes(), before)

    def test_a_fact_the_profile_already_has_is_never_overwritten(self):
        profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Phone: 555-0100-22\n\n## Notes\n- Full name: Alex Rivera\n- Pronouns:\n')
        for label, reply in (('Phone', '555-0199-99'), ('Full name', 'Someone Else')):
            self.assertFalse(self.ask_profile(tools, label, reply)['saved'], label)
        self.assertEqual(self.ask_profile(tools, 'Pronouns', 'he/him'), {'saved': True})
        self.assertFalse(self.ask_profile(tools, 'pronouns', 'they/them')['saved'])
        self.assertEqual(profile.read_text(), '# Application profile\n\n## Identity and contact\n- Phone: 555-0100-22\n\n## Notes\n- Full name: Alex Rivera\n- Pronouns: he/him\n')

    def test_a_link_label_in_any_wording_fills_the_profile_link(self):
        from jobfeed.my_profile_document import PersonalProfileStore
        for label in ('github_url', 'GitHub URL', 'Github Link'):
            profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Legal name: Alexandra Rivera\n')
            self.assertEqual(self.ask_profile(tools, label, 'https://github.com/example'), {'saved': True}, label)
            state = PersonalProfileStore(profile, self.root / 'review.json').state()
            self.assertEqual(state.personal_details.github_url, 'https://github.com/example', label)
            self.assertEqual(profile.read_text().count('github.com/example'), 1, label)

    def test_common_email_and_phone_wordings_fill_the_profile_fields_instead_of_adding_lines(self):
        from jobfeed.my_profile_document import PersonalProfileStore
        profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Legal name: Alexandra Rivera\n')
        self.assertEqual(self.ask_profile(tools, 'Email address', 'me@example.com'), {'saved': True})
        self.assertEqual(self.ask_profile(tools, 'Phone number', '+1 555-010-0222'), {'saved': True})
        self.assertFalse(self.ask_profile(tools, 'E-mail', 'other@example.com')['saved'])
        details = PersonalProfileStore(profile, self.root / 'review.json').state().personal_details
        self.assertEqual((details.primary_email, details.phone), ('me@example.com', '+1 555-010-0222'))
        self.assertNotIn('Email address:', profile.read_text())

    def test_a_preference_is_left_to_my_profile_so_a_no_reply_is_not_mistaken_for_junk(self):
        profile, tools = self.profile_tools('# Application profile\n\n## Identity and contact\n- Legal name: Alexandra Rivera\n')
        before = profile.read_bytes()
        for label in ('Relocation', 'Sponsorship requirement'):
            saved = self.ask_profile(tools, label, 'No')
            self.assertEqual((saved['saved'], 'My Profile' in saved['reason']), (False, True), label)
        self.assertEqual(profile.read_bytes(), before)

    def test_a_credential_is_never_saved_to_my_profile(self):
        profile, tools = self.profile_tools('# Application profile\n')
        for label in ('Password', 'Account passcode', 'SSN'):
            saved = self.ask_profile(tools, label, 'hunter2-secret')
            self.assertFalse(saved['saved'], label)
        self.assertEqual(profile.read_text(), '# Application profile\n')

    def answer_next_question(self, text, intent='answer'):
        def answer():
            while not (requests := self._open_requests()):
                time.sleep(0.02)
            self.store.reply(requests[0], text, source_kind='discord', source_id=f'msg-{uuid.uuid4().hex}', sender_id='user-1', intent=intent)
        threading.Thread(target=answer, daemon=True).start()

    def test_a_reply_to_a_form_question_is_saved_so_the_next_applier_finds_it(self):
        question = 'Are you authorized to work for any employer in the United States?'
        self.answer_next_question('Yes')
        result = ApplierTools(self.store, 1, 'owner-1', company='Valon').ask_user(
            'Valon asks about work authorization. What should I answer?', field='Work authorization', form_question=question, applies_to='every_company', suggestions=['Yes', 'No'])
        self.assertEqual((result['reply'], result['answer_saved']['saved']), ('Yes', True))
        self.store.bind(2, 'owner-2', 'batch')
        self.assertEqual(ApplierTools(self.store, 2, 'owner-2', company='Other Co').lookup_answer(question)['answer'], 'Yes')

    def test_a_this_company_reply_is_reused_only_for_that_company(self):
        self.answer_next_question('Your mission')
        ApplierTools(self.store, 1, 'owner-1', company='Valon').ask_user('Why Valon?', form_question='Why do you want to join us?', suggestions=['Your mission', 'Your product'])
        self.assertEqual(ApplierTools(self.store, 1, 'owner-1', company='Valon').lookup_answer('Why do you want to join us?')['answer'], 'Your mission')
        self.assertFalse(ApplierTools(self.store, 1, 'owner-1', company='Other Co').lookup_answer('Why do you want to join us?')['allowed'])

    def test_a_question_with_fixed_choices_offers_them_to_the_user(self):
        schema = next(tool for tool in TOOLS if tool['name'] == 'ask_user')['inputSchema']['properties']['options']
        self.assertEqual((schema['minItems'], schema['maxItems']), (2, 20))
        self.answer_next_question('No')
        result = ApplierTools(self.store, 1, 'owner-1', company='Visa').ask_user('Will you need sponsorship?', options=['Yes', 'No'])
        self.assertEqual((result['reply'], self.store.request(result['request_id'])['options']), ('No', ['Yes', 'No']))

    def test_a_question_without_choices_is_refused_before_the_user_is_asked(self):
        # A set stop keeps a regression from waiting forever for a reply that never comes.
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ValueError):
            ApplierTools(self.store, 1, 'owner-1', company='Valon').ask_user('Why Valon?', form_question='Why Valon?', stop=stop)
        self.assertEqual(self.store.requests(), [])
        failed, result = call(self.server, 'ask_user', {'question': 'What is your start date?', 'field': 'Start date'})
        self.assertTrue(failed)
        self.assertIn('suggestions', result['error'])
        self.assertEqual(self.store.requests(), [])

    def test_a_typed_answer_to_suggestions_is_saved_but_a_typed_answer_outside_the_form_options_is_not(self):
        tools = ApplierTools(self.store, 1, 'owner-1', company='Pave')
        language = 'Which programming language do you use most?'
        self.answer_next_question('Rust')
        result = tools.ask_user(language, field='Language', form_question=language, applies_to='every_company', suggestions=['Python', 'Go'])
        self.assertEqual(self.store.request(result['request_id'])['options'], ['Python', 'Go'])
        self.assertEqual((result['reply'], result['answer_saved']['saved']), ('Rust', True))
        self.assertEqual(tools.lookup_answer(language)['answer'], 'Rust')
        office = 'Which office would you work from?'
        self.answer_next_question('Boston')
        result = tools.ask_user(office, field='Office', form_question=office, applies_to='every_company', options=['New York', 'Remote'])
        self.assertEqual((result['reply'], result['answer_saved']['saved']), ('Boston', False))
        self.assertFalse(tools.lookup_answer(office)['allowed'])

    def test_an_approved_draft_is_saved_like_any_reply(self):
        self.answer_next_question('Your mortgage platform')
        result = ApplierTools(self.store, 1, 'owner-1', company='Valon').ask_user(
            'Why Valon?', form_question='Why Valon?', draft='Your mortgage platform')
        self.assertEqual(self.store.request(result['request_id'])['draft'], 'Your mortgage platform')
        self.assertEqual(ApplierTools(self.store, 1, 'owner-1', company='Valon').lookup_answer('Why Valon?')['answer'], 'Your mortgage platform')

    def test_feedback_on_a_draft_saves_nothing_and_the_approved_rewrite_is_saved(self):
        tools = ApplierTools(self.store, 1, 'owner-1', company='Valon')
        ask = dict(question='Why Valon?', field='Why Valon', form_question='Why do you want to work at Valon?')
        self.answer_next_question('Too formal. Mention the payments rewrite.', intent='change_request')
        result = tools.ask_user(**ask, draft='I am eager to contribute to Valon.')
        self.assertEqual({k: result[k] for k in ('feedback', 'draft')},
                         {'feedback': 'Too formal. Mention the payments rewrite.', 'draft': 'I am eager to contribute to Valon.'})
        self.assertIn('call ask_user again', result['next'])
        self.assertNotIn('reply', result)
        self.assertFalse(tools.lookup_answer(ask['form_question'])['allowed'])
        self.store.bind(2, 'owner-2', 'batch')
        other = StdioServer(ApplierTools(self.store, 2, 'owner-2'))
        other.response({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        failed, listed = call(other, 'get_draft_feedback', {})
        self.assertFalse(failed)
        self.assertEqual([{k: row[k] for k in ('feedback', 'question', 'draft', 'company', 'job_id')} for row in listed['feedback']],
                         [{'feedback': 'Too formal. Mention the payments rewrite.', 'question': 'Why Valon?',
                           'draft': 'I am eager to contribute to Valon.', 'company': 'Valon', 'job_id': 1}])
        rewrite = "I rewrote our payments service last year, and Valon's servicing platform has the same kind of problem."
        self.answer_next_question(rewrite)
        result = tools.ask_user(**ask, draft=rewrite)
        self.assertEqual((result['reply'], result['answer_saved']['saved']), (rewrite, True))
        self.assertEqual(tools.lookup_answer(ask['form_question'])['answer'], rewrite)

    def test_draft_feedback_comes_newest_first_up_to_the_limit(self):
        for n in range(3):
            request = self.store.ask(1, 'owner-1', 'question', f'Question {n}?', draft=f'Draft {n}.')
            self.store.reply(request, f'Feedback {n}', source_kind='discord', source_id=f'msg-{n}', sender_id='user-1', intent='change_request')
        self.store.reply(self.store.ask(1, 'owner-1', 'question', 'Other?', draft='Other.'), 'Other.', source_kind='discord', source_id='msg-a', sender_id='user-1')
        failed, listed = call(self.server, 'get_draft_feedback', {'limit': 2})
        self.assertFalse(failed)
        self.assertEqual([row['feedback'] for row in listed['feedback']], ['Feedback 2', 'Feedback 1'])
        self.assertEqual(len(call(self.server, 'get_draft_feedback', {})[1]['feedback']), 3)

    def test_without_an_exact_match_lookup_lists_related_answers_in_reach(self):
        for question, answer in (('Do you have professional experience with Python?', 'Yes'),
                                 ('How many years of professional experience with Python do you have?', '3')):
            self.answer_next_question(answer)
            ApplierTools(self.store, 1, 'owner-1', company='Pave').ask_user(question, form_question=question, applies_to='every_company', suggestions=LIKELY)
        self.answer_next_question('Your culture')
        ApplierTools(self.store, 1, 'owner-1', company='Pave').ask_user('Why Pave?', form_question='Why do you want to work at Pave?', suggestions=['Your mission', 'Your product'])
        self.store.bind(2, 'owner-2', 'batch')
        result = ApplierTools(self.store, 2, 'owner-2', company='Tech Systems').lookup_answer('Do you have Python experience in a professional setting?')
        self.assertFalse(result['allowed'])
        self.assertEqual(sorted((r['answer'], r['scope']) for r in result['related']), [('3', 'global'), ('Yes', 'global')])
        self.assertNotIn('related', ApplierTools(self.store, 2, 'owner-2', company='Tech Systems').lookup_answer('What is your favorite color?'))

    def test_a_question_without_form_wording_saves_nothing(self):
        self.answer_next_question('June 1')
        result = ApplierTools(self.store, 1, 'owner-1', company='Valon').ask_user('Start date?', suggestions=['June 1', 'July 1'])
        self.assertNotIn('answer_saved', result)
        self.assertFalse((self.root / '.pipeline/answer-bank.json').exists())

    def test_lookup_profile_reads_the_application_profile(self):
        (self.root / 'application-profile.md').write_text('# Contact\n\n- Email: synthetic@example.test\n- Phone: 555-0100\n')
        result = ApplierTools(self.store, 1, 'owner-1', company='Rilla').lookup_profile('Email')
        self.assertIn('synthetic@example.test', json.dumps(result))

    def tools(self, job_id, company):
        self.store.bind(job_id, f'owner-{job_id}', 'batch')
        return ApplierTools(self.store, job_id, f'owner-{job_id}', company=company)

    def test_record_answer_needs_auto_submit_mode(self):
        with self.assertRaisesRegex(ValueError, 'Auto submit mode is off'):
            self.tools(1, 'Judi Health').record_answer('Have you previously been employed by Judi Health?', 'No')
        self.assertFalse((self.root / '.pipeline/answer-bank.json').exists())

    def test_an_answer_the_applier_decided_is_reused_by_later_appliers_at_its_scope(self):
        self.store.set_auto_submit(True, actor='user via test', source='test')
        employer = 'Have you previously been employed by Judi Health?'
        self.assertTrue(self.tools(1, 'Judi Health').record_answer(employer, 'No', 'this_company')['saved'])
        self.assertTrue(self.tools(1, 'Judi Health').record_answer('What certifications and licenses do you have?', 'N/A', 'every_company')['saved'])
        self.assertEqual(self.tools(2, 'Judi Health').lookup_answer(employer)['answer'], 'No')
        self.assertFalse(self.tools(3, 'Other Co').lookup_answer(employer)['allowed'])
        self.assertEqual(self.tools(3, 'Other Co').lookup_answer('What certifications and licenses do you have?')['answer'], 'N/A')
        approval = AnswerBank(self.root).read()['entries'][0]['versions'][-1]['approval']
        self.assertEqual((approval['source']['kind'], approval['source']['job_id']), ('applier_auto', '1'))

    def test_an_applier_never_replaces_or_outranks_the_users_answer(self):
        self.store.set_auto_submit(True, actor='user via test', source='test')
        bank = AnswerBank(self.root)
        bank.learn('How did you hear about us?', 'Company website', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        for applies_to in ('every_company', 'this_company'):
            result = self.tools(1, 'Sierra').record_answer('How did you hear about us?', 'A referral', applies_to)
            self.assertEqual(result['saved'], False)
            self.assertIn('Company website', result['reason'])
        self.assertEqual(self.tools(1, 'Sierra').lookup_answer('How did you hear about us?')['answer'], 'Company website')
        self.tools(1, 'Sierra').record_answer('Why are you excited to join us at Sierra?', 'Your agent platform', 'this_company')
        bank.learn('Why are you excited to join us at Sierra?', 'The support work', {'kind': 'global'}, request_id='r2', reply_version=1, channel='discord')
        self.assertEqual(self.tools(2, 'Sierra').lookup_answer('Why are you excited to join us at Sierra?')['answer'], 'The support work')
        self.assertFalse(self.tools(2, 'Sierra').record_answer('Why are you excited to join us at Sierra?', 'Something else', 'every_company')['saved'])
        deleted = bank.learn('Name pronunciation', 'VIK-ter', {'kind': 'global'}, request_id='r3', reply_version=1, channel='discord')
        bank.mutate({'action': 'revoke', 'entry_id': deleted['id'], 'expected_version': 1, 'expected_revision': bank.read()['revision']})
        self.assertFalse(self.tools(1, 'Sierra').record_answer('Name pronunciation', 'Alex', 'every_company')['saved'])
        self.assertEqual(next(e for e in bank.read()['entries'] if e['id'] == deleted['id'])['versions'][-1]['state'], 'revoked')

    def test_a_decided_answer_to_a_common_question_is_saved_to_my_profile(self):
        self.store.set_auto_submit(True, actor='user via test', source='test')
        self.tools(1, 'Sierra').record_answer('What is your gender identity?', 'Decline to answer', common_question='gender')
        self.assertEqual(self.tools(2, 'Other Co').lookup_answer('Gender', common_question='gender')['answer'], 'Decline to answer')

    def test_a_job_not_found_page_is_reported_closed(self):
        self.assertEqual(self.tools(1, 'Finch Legal').report_closed('Job not found. The job you requested was not found.')['recorded'], True)
        self.assertEqual(self.store.skip(1)['reason'], 'Job not found. The job you requested was not found.')

    def _open_requests(self):
        if not self.store.path.exists():
            return []
        with sqlite3.connect(self.store.path) as db:
            return [row[0] for row in db.execute("SELECT id FROM applier_requests WHERE state='open'")]


if __name__ == '__main__':
    unittest.main()
