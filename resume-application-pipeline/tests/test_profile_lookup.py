import tempfile
import unittest
from pathlib import Path

from pipeline.profile_lookup import MaterialSource, LookupQuery, lookup_confirmed_material


class ProfileLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def source(self, name, text, **kw):
        path = Path(self.temp.name) / name
        path.write_text(text)
        return MaterialSource(path, name, **kw)

    def query(self, **kw):
        return LookupQuery('relocation', ('mobility',), **kw)

    def test_minimal_material_and_provenance(self):
        src = self.source('profile', '# Mobility\nRelocation: funded preferred, self funded acceptable\n# Other\n' + 'irrelevant\n' * 200,
                          categories={'mobility': ['Mobility']})
        result = lookup_confirmed_material([src], self.query())
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['snippets'][0]['line_start'], 2)
        self.assertEqual(len(result['snippets'][0]['version']), 64)
        self.assertLess(result['material_size']['snippet_chars'], 100)
        self.assertTrue(result['material_size']['token_count_is_estimate'])

    def test_index_gap_is_not_missing_fact(self):
        src = self.source('profile', '# 移居\n搬迁: 可以自费\n')
        result = lookup_confirmed_material([src], self.query())
        self.assertEqual(result['status'], 'coverage_incomplete')
        self.assertFalse(result['missing_fact_confirmed'])
        self.assertEqual(result['unindexed_categories'], ['mobility'])

    def test_cross_company_fact_excluded(self):
        src = self.source('terms', '# relocation\nConsent: yes\n', scope={'company': 'Alpha'})
        result = lookup_confirmed_material([src], self.query(scope={'company': 'Beta'}))
        self.assertEqual(result['snippets'], [])
        self.assertEqual(result['coverage'][0]['status'], 'out_of_scope')

    def test_conflict_and_explicit_override(self):
        a = self.source('old', '# Mobility\nRelocation: no\n', categories={'mobility': ['Mobility']})
        b = self.source('new', '# Mobility\nRelocation: yes\n', categories={'mobility': ['Mobility']})
        self.assertEqual(lookup_confirmed_material([a,b], self.query())['status'], 'conflict')
        b = MaterialSource(b.path, b.source_id, categories=b.categories, supersedes=('old',))
        result = lookup_confirmed_material([a,b], self.query())
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['snippets'][0]['source_id'], 'new')

    def test_credentials_not_returned_even_when_matching_query(self):
        src = self.source('profile', '# 账号\nrelocation: DO_NOT_RETURN\n## nested\nplain SECRET_VALUE\n# Mobility\nRelocation: yes\nPassword: BAD_VALUE\n', categories={'mobility': ['Mobility']})
        result = lookup_confirmed_material([src], self.query())
        self.assertNotIn('DO_NOT_RETURN', str(result))
        self.assertNotIn('SECRET_VALUE', str(result))
        self.assertNotIn('BAD_VALUE', str(result))
        self.assertEqual(len(result['snippets']), 1)

    def test_budget_and_failed_source_need_review(self):
        src = self.source('profile', '# Mobility\nRelocation: ' + 'yes ' * 100, categories={'mobility': ['Mobility']})
        result = lookup_confirmed_material([src], self.query(max_chars=10))
        self.assertTrue(result['truncated'])
        self.assertEqual(result['status'], 'coverage_incomplete')
        result = lookup_confirmed_material([MaterialSource('/nonexistent/fixture', 'absent')], self.query())
        self.assertEqual(result['status'], 'coverage_incomplete')
        self.assertEqual(result['errors'][0]['error'], 'FileNotFoundError')

    def test_appended_answer_is_retrievable_only_for_its_job_with_message_source(self):
        src = self.source('profile', '# Application profile\n'
            '## Confirmed user answer — job 7\n\nScope: job:7\nSource: thread-7 / message-7\n'
            'User text: "Relocation: JOB_SEVEN_ANSWER"\n'
            '## Confirmed user answer — job 8\n\nScope: job:8\nSource: thread-8 / message-8\n'
            'User text: "Relocation: JOB_EIGHT_ANSWER"\n')
        seven = lookup_confirmed_material([src], self.query(scope={'job':'7'}))
        self.assertIn('JOB_SEVEN_ANSWER', str(seven['snippets']))
        self.assertNotIn('JOB_EIGHT_ANSWER', str(seven))
        self.assertEqual(seven['snippets'][0]['scope'], {'job':'7'})
        self.assertEqual(seven['snippets'][0]['user_source'], 'thread-7 / message-7')
        self.assertEqual(lookup_confirmed_material([src], self.query())['snippets'], [])
        self.assertEqual(lookup_confirmed_material([src], self.query(scope={'job':'9'}))['snippets'], [])

    def test_conflicting_appended_scope_never_exposes_answer(self):
        src = self.source('profile', '## Confirmed user answer — job 7\nScope: job:8\nRelocation: NEVER_REUSE\n')
        for job in ('7','8'):
            result = lookup_confirmed_material([src], self.query(scope={'job':job}))
            self.assertNotIn('NEVER_REUSE', str(result))

    def test_contact_facts_are_found_under_the_form_label(self):
        from types import SimpleNamespace
        from pipeline.profile_lookup import job_material
        (Path(self.temp.name) / 'application-profile.md').write_text(
            '# Application profile\n\n## Identity and contact\n- GitHub: https://github.com/example\n'
            '- Preferred name: Sam\n- Legal name: Samuel Example\n')
        job = SimpleNamespace(id=1, company='Acme')
        for label in ('GitHub URL', 'Github Link', 'First name'):
            text = str(job_material(self.temp.name, job, specification={'aliases': [label]})['snippets'])
            self.assertIn('github.com/example', text, label)
            self.assertIn('Preferred name: Sam', text, label)
            self.assertIn('Legal name: Samuel Example', text, label)


if __name__ == '__main__':
    unittest.main()
