import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline.answer_bank import AnswerBank, BankError


class AnswerBankTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = AnswerBank(self.root)
        self.draft = {'question': 'What did you build?', 'answer': 'I built a renderer.',
                      'aliases': ['Describe your best engineering project.'],
                      'scope': {'kind': 'company', 'company': 'Acme'}, 'valid_until': None}
        self.context = {'job_id': '1', 'company': 'Acme'}

    def test_a_one_word_label_finds_a_saved_answer_that_uses_the_word(self):
        # 2026-11-18: a form labelled "Citizenship" never matched "In which country/region do you have citizenship?".
        self.draft = dict(self.draft, question='In which country/region do you have citizenship?', answer='Canada', aliases=[],
                          scope={'kind': 'global'})
        self.create()
        self.approve()
        self.assertEqual([m['answer'] for m in self.bank.related('Citizenship', self.context)], ['Canada'])
        self.assertEqual(self.bank.related('Country of birth', self.context), [], 'two words still need two in common')

    def test_a_label_worded_differently_finds_the_saved_answer_first(self):
        # 2026-11-18: a form asking "Nationality" missed the saved citizenship answer and asked the user again.
        self.draft = dict(self.draft, question='In which country/region do you have citizenship?', answer='Canada', aliases=[],
                          scope={'kind': 'global'})
        self.create()
        self.approve()
        for label in ('Nationality', 'Citizenship', 'Country of citizenship'):
            with self.subTest(label):
                self.assertEqual([m['answer'] for m in self.bank.related(label, self.context)][:1], ['Canada'])
        self.assertEqual(self.bank.related('Member of the National Guard?', self.context), [], 'a national guard is not a nationality')

    def test_salary_and_compensation_wordings_meet(self):
        self.draft = dict(self.draft, question='What is your desired salary?', answer='120,000', aliases=[], scope={'kind': 'global'})
        self.create()
        self.approve()
        self.assertEqual([m['answer'] for m in self.bank.related('Expected compensation', self.context)], ['120,000'])

    def create(self):
        return self.bank.mutate({'action': 'create', 'entry_id': 'project', 'expected_revision': 0, 'draft': self.draft})

    def approve(self):
        state = self.bank.read()
        return self.bank.mutate({'action': 'approve', 'entry_id': 'project', 'expected_revision': state['revision'], 'expected_version': state['entries'][0]['current_version']}, dashboard=True)

    def test_new_answer_requires_exact_approval_and_alias_scope(self):
        self.create()
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context)['allowed'])
        state = self.approve()
        approval = state['entries'][0]['versions'][0]['approval']
        self.assertEqual(approval['source'], {'kind': 'dashboard', 'actor': 'local-user'})
        self.assertTrue(approval['event_id'])
        self.assertEqual(self.bank.resolve('  What did you build? ', self.context)['answer'], self.draft['answer'])
        self.assertTrue(self.bank.resolve(self.draft['aliases'][0], self.context)['allowed'])
        self.assertFalse(self.bank.resolve('What is your best project?', self.context)['allowed'])
        self.assertFalse(self.bank.resolve(self.draft['question'], {'company': 'Other', 'job_id': '1'})['allowed'])
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context, answer='I built a different renderer.')['allowed'])
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context, char_limit=5)['allowed'])
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context, word_limit=2)['allowed'])

    def test_the_newest_of_two_approved_answers_to_one_question_wins(self):
        self.create()
        self.approve()
        self.bank.mutate({'action': 'create', 'entry_id': 'duplicate', 'expected_revision': 2, 'draft': dict(self.draft, answer='I built a compiler.')})
        self.bank.mutate({'action': 'approve', 'entry_id': 'duplicate', 'expected_revision': 3, 'expected_version': 1}, dashboard=True)
        result = self.bank.resolve(self.draft['question'], self.context)
        self.assertEqual((result['entry_id'], result['answer']), ('duplicate', 'I built a compiler.'))

    def test_a_reply_to_an_applier_question_is_approved_and_reused_by_later_jobs(self):
        question = 'Do you now, or will you in the future, require sponsorship?'
        entry = self.bank.learn(question, 'Yes', {'kind': 'global'}, request_id='req-1', reply_version=1, channel='discord')
        approval = entry['versions'][0]['approval']
        self.assertEqual((entry['versions'][0]['state'], approval['event_id'], approval['source']['kind']), ('approved', 'req-1:1', 'user_reply'))
        self.assertEqual(self.bank.resolve(question, {'company': 'Other Co', 'job_id': '9'})['answer'], 'Yes')
        self.assertEqual(self.bank.learn(question, 'Yes', {'kind': 'global'}, request_id='req-2', reply_version=1, channel='discord'), entry)
        changed = self.bank.learn(question, 'No', {'kind': 'global'}, request_id='req-3', reply_version=1, channel='discord')
        self.assertEqual([v['state'] for v in changed['versions']], ['superseded', 'approved'])
        self.assertEqual(self.bank.resolve(question, self.context)['answer'], 'No')
        self.assertEqual(len(self.bank.read()['entries']), 1)

    def test_a_company_answer_overrides_a_global_answer_for_that_company(self):
        question = 'Why do you want to work here?'
        self.bank.learn(question, 'General reason', {'kind': 'global'}, request_id='req-1', reply_version=1, channel='discord')
        self.bank.learn(question, 'Acme reason', {'kind': 'company', 'company': 'Acme'}, request_id='req-2', reply_version=1, channel='discord')
        self.assertEqual(self.bank.resolve(question, self.context)['answer'], 'Acme reason')
        self.assertEqual(self.bank.resolve(question, {'company': 'Other Co', 'job_id': '2'})['answer'], 'General reason')

    def test_proposal_uses_bound_job_and_rejects_outside_folder(self):
        folder = self.root / 'applications/job-1'
        folder.mkdir(parents=True)
        (folder / 'application-task.json').write_text(json.dumps({'company': 'Acme', 'job_id': 1}))
        entry = self.bank.propose(str(folder), 'Why Acme?', 'A draft')
        self.assertEqual(entry['versions'][0]['scope'], {'kind': 'job', 'company': 'Acme', 'job_id': '1'})
        self.assertEqual(entry['versions'][0]['state'], 'pending_review')
        with self.assertRaises(BankError):
            self.bank.propose(str(self.root), 'Why?', 'Draft')

    def test_edit_invalidates_old_approval_and_replay_is_idempotent(self):
        self.create()
        self.approve()
        request = {'action': 'edit', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': 2,
                   'draft': {**self.draft, 'answer': 'I built a reusable renderer.'}}
        state = self.bank.mutate(request)
        self.assertEqual(self.bank.mutate(request), state)
        self.assertEqual(state['entries'][0]['current_version'], 2)
        self.assertEqual([v['state'] for v in state['entries'][0]['versions']], ['superseded', 'pending_review'])
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context, answer=self.draft['answer'])['allowed'])
        with self.assertRaises(BankError):
            self.bank.mutate({'action': 'approve', 'entry_id': 'project', 'expected_revision': 3, 'expected_version': 1}, dashboard=True)
        self.approve()
        self.assertEqual(self.bank.resolve(self.draft['question'], self.context)['version'], 2)

    def test_generic_cli_cannot_approve_or_forge_source(self):
        self.create()
        with self.assertRaisesRegex(BankError, 'local dashboard'):
            self.bank.mutate({'action': 'approve', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': 1})
        with self.assertRaisesRegex(BankError, 'Unsupported'):
            self.bank.mutate({'action': 'approve', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': 1, 'source': {'kind': 'dashboard'}}, dashboard=True)

    def test_stale_edits_conflict_and_approval_revocation_is_idempotent(self):
        self.create()
        state = self.approve()
        request = {'action': 'revoke', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': state['revision']}
        state = self.bank.mutate(request)
        self.assertEqual(self.bank.mutate(request), state)
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context)['allowed'])
        with self.assertRaises(BankError) as error:
            self.bank.mutate({'action': 'edit', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': 1, 'draft': {**self.draft, 'answer': 'changed'}})
        self.assertEqual(error.exception.code, 'conflict')

    def test_alias_or_scope_edit_requires_new_review(self):
        self.create()
        self.approve()
        self.bank.mutate({'action': 'edit', 'entry_id': 'project', 'expected_version': 1, 'expected_revision': 2,
                          'draft': {**self.draft, 'aliases': ['What is your best project?'], 'scope': {'kind': 'global'}}})
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context)['allowed'])

    def test_job_scope_expiry_and_missing_context_fail_closed(self):
        self.draft['scope'] = {'kind': 'job', 'company': 'Acme', 'job_id': '1'}
        self.create()
        self.approve()
        self.assertFalse(self.bank.resolve(self.draft['question'], {'company': 'Acme', 'job_id': '2'})['allowed'])
        self.assertFalse(self.bank.resolve(self.draft['question'], {})['allowed'])
        state = self.bank.read()
        state['entries'][0]['versions'][0]['valid_until'] = '2001-01-01'
        self.bank.path.write_text(json.dumps(state))
        self.assertFalse(self.bank.resolve(self.draft['question'], self.context)['allowed'])

    def test_expiry_requires_an_explicit_timezone_or_a_date(self):
        for value in ['2026-12-01T10:00:00', '2026-02-30', 'December 1 2026']:
            with self.subTest(value=value), self.assertRaises(BankError):
                self.bank.mutate({'action': 'create', 'expected_revision': 0, 'draft': {**self.draft, 'valid_until': value}})

    def test_missing_and_malformed_banks_fail_closed(self):
        with self.assertRaises(BankError):
            self.bank.resolve(self.draft['question'], self.context)
        self.create()
        self.approve()
        original = self.bank.read()
        for mutator in (lambda s: s.update(schema_version=2),
                        lambda s: s['entries'][0]['versions'][0].pop('aliases'),
                        lambda s: s['entries'][0]['versions'][0]['approval'].update(source='forged'),
                        lambda s: s['entries'][0]['versions'][0]['approval'].update(version=2)):
            state = copy.deepcopy(original)
            mutator(state)
            self.bank.path.write_text(json.dumps(state))
            with self.assertRaises(BankError):
                self.bank.resolve(self.draft['question'], self.context)
        self.bank.path.write_text('not json')
        with self.assertRaises(BankError):
            self.bank.read()

    def test_import_preserves_rejections_and_never_imports_approvals(self):
        row = {'id': 'project', 'version': 2, 'question': self.draft['question'], 'answer': self.draft['answer'], 'status': 'approved'}
        old = {**row, 'version': 1, 'answer': 'Old prose', 'status': 'revision_requested'}
        review = {'company': 'Acme', 'job_id': 1, 'entries': [row], 'history': [old]}
        state = self.bank.import_review(review)
        self.assertEqual(self.bank.import_review(review), state)
        versions = state['entries'][0]['versions']
        self.assertEqual(versions[0]['imported_status'], 'revision_requested')
        self.assertEqual(versions[1]['state'], 'pending_review')
        self.assertIsNone(versions[1]['approval'])

    def test_concurrent_updates_cannot_lose_one_another(self):
        self.create()
        def edit(answer):
            try:
                self.bank.mutate({'action': 'edit', 'entry_id': 'project', 'expected_revision': 1, 'expected_version': 1, 'draft': {**self.draft, 'answer': answer}})
                return 'saved'
            except BankError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, ['First edit', 'Second edit']))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(self.bank.read()['revision'], 2)
        self.assertEqual(self.bank.path.stat().st_mode & 0o777, 0o600)

    def test_create_retry_with_stable_id_and_pending_deduplication(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        one = self.bank.pending('Another question?', 'Draft response', self.context)
        two = self.bank.pending('Another question?', 'Draft response', self.context)
        self.assertEqual(one, two)
        self.assertEqual(len(self.bank.read()['entries']), 2)


class DashboardSaveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = AnswerBank(self.root)
        self.context = {'company': 'Acme', 'job_id': '7'}

    def save(self, answer, *, question='Are you at least 18 years old?', entry_id=None, expected_version=None, scope=None, revision=None):
        request = {'action': 'save', 'expected_revision': self.bank.read()['revision'] if revision is None else revision,
                   'draft': {'question': question, 'answer': answer, 'scope': scope or {'kind': 'global'}}}
        if entry_id:
            request['entry_id'] = entry_id
        if expected_version is not None:
            request['expected_version'] = expected_version
        return self.bank.mutate(request, dashboard=True)

    def test_saving_a_new_answer_approves_it_and_a_replay_changes_nothing(self):
        state = self.save('Yes', entry_id='adult')
        version = state['entries'][0]['versions'][0]
        self.assertEqual((version['state'], version['approval']['source']['kind']), ('approved', 'dashboard'))
        self.assertEqual(self.bank.resolve('Are you at least 18 years old?', self.context)['answer'], 'Yes')
        replay = self.save('Yes', entry_id='adult', revision=0)
        self.assertEqual(replay['revision'], state['revision'])
        self.assertEqual(len(replay['entries'][0]['versions']), 1)

    def test_saving_over_an_answer_keeps_its_history_and_uses_the_new_answer(self):
        self.save('Yes', entry_id='adult')
        state = self.save('No', entry_id='adult', expected_version=1)
        entry = state['entries'][0]
        self.assertEqual([(v['version'], v['state']) for v in entry['versions']], [(1, 'superseded'), (2, 'approved')])
        self.assertEqual(self.bank.resolve('Are you at least 18 years old?', self.context)['answer'], 'No')

    def test_saving_a_drafted_answer_approves_the_edited_text_for_its_job(self):
        drafted = self.bank.pending('Why Acme?', 'Drafted reason.', self.context)
        self.assertFalse(self.bank.resolve('Why Acme?', self.context)['allowed'])
        scope = drafted['versions'][0]['scope']
        state = self.save('My own reason.', question='Why Acme?', entry_id=drafted['id'], expected_version=1, scope=scope)
        versions = state['entries'][0]['versions']
        self.assertEqual((versions[0]['state'], versions[0]['superseded_state']), ('superseded', 'pending_review'))
        self.assertEqual(self.bank.resolve('Why Acme?', self.context)['answer'], 'My own reason.')
        self.assertFalse(self.bank.resolve('Why Acme?', {'company': 'Acme', 'job_id': '8'})['allowed'])

    def test_stale_saves_conflict(self):
        self.save('Yes', entry_id='adult')
        with self.assertRaises(BankError) as stale_revision:
            self.save('No', entry_id='adult', expected_version=1, revision=0)
        with self.assertRaises(BankError) as stale_version:
            self.save('No', entry_id='adult', expected_version=0)
        with self.assertRaises(BankError) as missing_version:
            self.save('No', entry_id='adult')
        self.assertEqual({stale_revision.exception.code, stale_version.exception.code, missing_version.exception.code}, {'conflict'})
        self.assertEqual(self.bank.resolve('Are you at least 18 years old?', self.context)['answer'], 'Yes')

    def test_only_the_dashboard_can_save(self):
        with self.assertRaisesRegex(BankError, 'local dashboard'):
            self.bank.mutate({'action': 'save', 'expected_revision': 0, 'draft': {'question': 'Gender', 'answer': 'Female', 'scope': {'kind': 'global'}}})
        self.assertEqual(self.bank.read()['entries'], [])

    def test_a_saved_common_question_answers_its_other_wordings(self):
        self.save('Decline to self-identify', question='What is your race or ethnicity?')
        for wording in ('Please identify your race', 'Ethnicity', 'Race/Ethnicity (required)'):
            self.assertEqual(self.bank.resolve(wording, self.context)['answer'], 'Decline to self-identify', wording)
        self.assertFalse(self.bank.resolve('Are you Hispanic or Latino?', self.context)['allowed'])

    def test_an_answer_to_one_wording_of_a_common_question_answers_the_others(self):
        self.bank.learn('Are you 18 or older?', 'Yes', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        self.assertEqual(self.bank.resolve('Are you over 18?', self.context)['answer'], 'Yes')
        self.bank.learn('Are you over 18?', 'No', {'kind': 'global'}, request_id='r2', reply_version=1, channel='discord')
        self.assertEqual(self.bank.resolve('Are you over 18?', self.context)['answer'], 'No')
        self.assertEqual(self.bank.resolve('Are you 18 or older?', self.context)['answer'], 'Yes')

    def test_a_saved_relocation_answer_answers_an_employer_specific_relocation_question(self):
        self.bank.learn('are you willing to relocate?', 'Yes', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        result = self.bank.resolve('This role requires that you are willing to relocate to San Francisco, CA. Are you willing?',
                                   {'company': 'Notion', 'job_id': '7'}, common_key='relocation')
        self.assertEqual(result['answer'], 'Yes')
        self.assertEqual(self.bank.resolve('Are you willing to relocate?', {'company': 'Notion', 'job_id': '7'})['answer'], 'Yes')

    def test_the_newest_answer_to_any_wording_of_a_common_question_wins(self):
        self.bank.learn('Do you now, or will you at any point in the future, require sponsorship?', 'Yes', {'kind': 'global'},
                        request_id='r1', reply_version=1, channel='discord')
        self.bank.learn('Will you now or at any time in the future require sponsorship for employment visa status (e.g. H1B, OPT)?', 'No',
                        {'kind': 'global'}, request_id='r2', reply_version=1, channel='discord')
        result = self.bank.resolve('Will you now or in the future require visa sponsorship to work in the United States?', self.context)
        self.assertEqual((result['allowed'], result['answer']), (True, 'No'))

    def test_a_company_answer_to_the_exact_question_beats_the_global_common_answer_for_that_company(self):
        question = 'Are you able to work from our SF office five days per week?'
        self.bank.learn('Are you willing to work onsite in the office?', 'Yes', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        self.bank.learn(question, 'No', {'kind': 'company', 'company': 'Netic'}, request_id='r2', reply_version=1, channel='discord')
        self.assertEqual(self.bank.resolve(question, {'company': 'Netic', 'job_id': '3'}, common_key='onsite')['answer'], 'No')
        self.assertEqual(self.bank.resolve(question, {'company': 'Chalk', 'job_id': '4'}, common_key='onsite')['answer'], 'Yes')

    def test_every_catalog_wording_belongs_to_one_question(self):
        from pipeline.answer_bank import common_question
        from pipeline.common_questions import CATALOG
        for item in CATALOG:
            for wording in (item.question, *item.aliases):
                self.assertIs(common_question(wording), item, wording)
            self.assertIn(item.group, {'demographic', 'application'})

    def test_list_shows_the_catalog_with_the_saved_answer_for_each_question(self):
        import subprocess
        import sys
        self.save('Female', question='Gender', entry_id='gender')
        self.bank.pending('What is your gender?', 'Drafted', self.context)
        result = subprocess.run([sys.executable, '-m', 'pipeline.answer_bank', '--root', str(self.root), 'list'], input='{}',
                                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], check=True)
        catalog = {item['key']: item for item in json.loads(result.stdout)['common_questions']}
        self.assertEqual(catalog['gender']['entry_id'], 'gender')
        self.assertEqual(catalog['gender']['options'][-1], 'Decline to self-identify')
        self.assertIsNone(catalog['race']['entry_id'])
        self.assertEqual(catalog['pronouns']['options'], [])
        self.assertIn('Ethnicity', catalog['race']['aliases'])


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bank = AnswerBank(Path(self.temp.name))
        self.context = {'company': 'Acme', 'job_id': '7'}

    def save(self, entry_id, question, answer, scope=None):
        self.bank.mutate({'action': 'save', 'entry_id': entry_id, 'expected_revision': self.bank.read()['revision'],
                          'draft': {'question': question, 'answer': answer, 'scope': scope or {'kind': 'global'}}}, dashboard=True)

    def merge(self, target, sources, revision=None):
        return self.bank.mutate({'action': 'merge', 'entry_id': target, 'merge_entry_ids': sources,
                                 'expected_revision': self.bank.read()['revision'] if revision is None else revision}, dashboard=True)

    def entry(self, entry_id):
        return next(e for e in self.bank.read()['entries'] if e['id'] == entry_id)

    def test_merge_folds_wordings_into_the_kept_answer_and_revokes_the_others_with_their_history(self):
        kept = self.bank.learn('Name of your current employer', 'Example Corp', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')
        self.save('recent', 'Most recent employer', 'Example Corp')
        self.save('worked', 'Where have you most recently worked?', 'Example Corp')
        self.bank.mutate({'action': 'save', 'entry_id': 'worked', 'expected_revision': self.bank.read()['revision'], 'expected_version': 1,
                          'draft': {'question': 'Where have you most recently worked?', 'answer': 'Example Corp', 'aliases': ['Last company'],
                                    'scope': {'kind': 'global'}}}, dashboard=True)
        self.merge(kept['id'], ['recent', 'worked'])
        current = self.entry(kept['id'])['versions'][-1]
        self.assertEqual((current['state'], current['answer'], current['question']), ('approved', 'Example Corp', 'Name of your current employer'))
        self.assertEqual(current['aliases'], ['Most recent employer', 'Where have you most recently worked?', 'Last company'])
        self.assertEqual(current['approval']['source'], {'kind': 'user_reply', 'channel': 'discord', 'request_id': 'r1', 'merged_from': ['recent', 'worked']})
        for source, versions in (('recent', 2), ('worked', 3)):
            history = self.entry(source)['versions']
            self.assertEqual(len(history), versions, 'every earlier version is kept')
            self.assertEqual((history[-1]['state'], history[-1]['merged_into']), ('revoked', kept['id']))
            self.assertEqual(history[-2]['superseded_state'], 'approved')
        for wording in ('Most recent employer', 'Last company', 'where have you most recently worked'):
            found = self.bank.resolve(wording, self.context)
            self.assertEqual((found['entry_id'], found['answer']), (kept['id'], 'Example Corp'))
        self.assertEqual({m['entry_id'] for m in self.bank.related('Most recent employer', self.context)}, {kept['id']})

    def test_a_users_answer_still_outranks_an_applier_answer_after_a_merge(self):
        self.bank.decide('Current employer', 'Guessed Co', {'kind': 'global'}, self.context, owner='owner-7')
        self.save('user', 'Employer name', 'Example Corp')
        self.save('other', 'Current employer', 'Example Corp')
        self.merge('user', ['other'])
        self.assertEqual(self.bank.resolve('Current employer', self.context)['entry_id'], 'user')
        self.assertEqual(self.entry('user')['versions'][-1]['approval']['source']['kind'], 'dashboard')

    def test_an_applier_answer_stays_an_applier_answer_when_merged_into(self):
        decided = self.bank.decide('Preferred working hours', 'Daytime', {'kind': 'global'}, self.context, owner='owner-7')
        self.save('hours', 'What hours do you prefer to work?', 'Daytime')
        self.merge(decided['id'], ['hours'])
        source = self.entry(decided['id'])['versions'][-1]['approval']['source']
        self.assertEqual((source['kind'], source['job_id']), ('applier_auto', '7'))

    def test_only_answers_at_the_same_scope_merge(self):
        self.save('one', 'Why Acme?', 'Reason', {'kind': 'company', 'company': 'Acme'})
        self.save('two', 'Why join Acme?', 'Reason', {'kind': 'company', 'company': ' acme'})
        self.save('three', 'Why join us?', 'Reason')
        revision = self.bank.read()['revision']
        with self.assertRaises(BankError) as raised:
            self.merge('one', ['two', 'three'])
        self.assertEqual(raised.exception.code, 'invalid')
        self.assertEqual(self.bank.read()['revision'], revision)
        self.merge('one', ['two'])
        self.assertEqual(self.bank.resolve('Why join Acme?', self.context)['entry_id'], 'one')

    def test_merge_refuses_a_stale_revision_unsaved_answers_and_other_callers(self):
        self.save('one', 'First wording', 'Same')
        self.save('two', 'Second wording', 'Same')
        with self.assertRaises(BankError) as raised:
            self.merge('one', ['two'], revision=0)
        self.assertEqual(raised.exception.code, 'conflict')
        with self.assertRaises(BankError):
            self.bank.mutate({'action': 'merge', 'entry_id': 'one', 'merge_entry_ids': ['two'], 'expected_revision': self.bank.read()['revision']})
        self.bank.mutate({'action': 'revoke', 'entry_id': 'two', 'expected_version': 1, 'expected_revision': self.bank.read()['revision']})
        with self.assertRaises(BankError):
            self.merge('one', ['two'])
        for sources in ([], ['one'], ['missing']):
            with self.subTest(sources), self.assertRaises(BankError):
                self.merge('one', sources)

    def test_a_replayed_merge_changes_nothing(self):
        self.save('one', 'First wording', 'Same')
        self.save('two', 'Second wording', 'Same')
        before = self.bank.read()['revision']
        state = self.merge('one', ['two'], revision=before)
        replay = self.merge('one', ['two'], revision=before)
        self.assertEqual(replay, state)
        self.assertEqual(self.bank.read()['revision'], before + 1)

    def test_the_dashboard_command_merges(self):
        import subprocess
        import sys
        self.save('one', 'First wording', 'Same')
        self.save('two', 'Second wording', 'Same')
        request = {'action': 'merge', 'entry_id': 'one', 'merge_entry_ids': ['two'], 'expected_revision': self.bank.read()['revision']}
        result = subprocess.run([sys.executable, '-m', 'pipeline.answer_bank', '--root', self.temp.name, 'dashboard-mutate'], input=json.dumps(request),
                                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], check=True)
        self.assertNotIn('error', json.loads(result.stdout))
        self.assertEqual(self.bank.resolve('Second wording', self.context)['entry_id'], 'one')


class AddAliasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bank = AnswerBank(Path(self.temp.name))
        self.context = {'company': 'Acme', 'job_id': '7'}
        self.entry = self.bank.learn('In which country do you hold citizenship?', 'Canada', {'kind': 'global'}, request_id='r1', reply_version=1, channel='discord')

    def test_a_new_wording_resolves_exactly_to_the_same_answer_and_source(self):
        self.assertFalse(self.bank.resolve('Nationality', self.context)['allowed'])
        self.bank.add_alias(self.entry['id'], 'Nationality', 'Canada', self.context, owner='owner-7')
        found = self.bank.resolve('Nationality', self.context)
        self.assertEqual((found['entry_id'], found['answer']), (self.entry['id'], 'Canada'))
        current = self.bank.read()['entries'][0]['versions'][-1]
        self.assertEqual((current['question'], current['aliases']), ('In which country do you hold citizenship?', ['Nationality']))
        self.assertEqual(current['approval']['source'], {'kind': 'user_reply', 'channel': 'discord', 'request_id': 'r1',
                                                         'alias_added_by': {'owner': 'owner-7', 'job_id': '7'}})
        self.bank.add_alias(self.entry['id'], 'nationality?', 'Canada', self.context, owner='owner-8')
        self.assertEqual(len(self.bank.read()['entries'][0]['versions']), 2, 'a known wording adds nothing')

    def refused(self, *args, context=None):
        with self.assertRaises(BankError) as raised:
            self.bank.add_alias(*args, context or self.context, owner='owner-7')
        self.assertEqual(raised.exception.code, 'refused')

    def test_it_refuses_another_answer_an_out_of_reach_entry_and_unknown_entries(self):
        self.refused(self.entry['id'], 'Nationality', 'Canadian')
        acme = self.bank.learn('Why Acme?', 'Reason', {'kind': 'company', 'company': 'Acme'}, request_id='r2', reply_version=1, channel='discord')
        self.refused(acme['id'], 'Why us?', 'Reason', context={'company': 'Other Co', 'job_id': '8'})
        self.refused('missing', 'Nationality', 'Canada')
        self.refused(self.entry['id'], 'Email', 'Canada')
        self.assertEqual([len(e['versions']) for e in self.bank.read()['entries']], [1, 1])

    def test_it_refuses_revoked_and_expired_answers(self):
        self.bank.mutate({'action': 'revoke', 'entry_id': self.entry['id'], 'expected_version': 1, 'expected_revision': self.bank.read()['revision']})
        self.refused(self.entry['id'], 'Nationality', 'Canada')
        expiring = self.bank.learn('Notice period', 'Two weeks', {'kind': 'global'}, request_id='r3', reply_version=1, channel='discord')
        store = json.loads(self.bank.path.read_text())
        next(e for e in store['entries'] if e['id'] == expiring['id'])['versions'][-1]['valid_until'] = '2020-01-01'
        self.bank.path.write_text(json.dumps(store))
        self.refused(expiring['id'], 'How much notice do you need?', 'Two weeks')
