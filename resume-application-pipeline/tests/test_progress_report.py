import unittest

from pipeline.progress_monitor import attach_resources
from pipeline.progress_report import render_dashboard


def snapshot():
    return dict(checked_at='2026-10-20T00:00:00Z', target=1,
                counts=dict(submitted=0, synced=0, closed=0, occupied=0), errors=[], jobs=[])


class ProgressReportTests(unittest.TestCase):
    def test_acquired_requests_no_longer_display_as_waiting(self):
        data = {'jobs': [{'job_id': 1}]}
        attach_resources(data, {'waits': [
            {'job_id': 1, 'request_id': 'done', 'status': 'acquired'},
            {'job_id': 1, 'request_id': 'pending', 'status': 'waiting'},
        ]})
        self.assertEqual([r['request_id'] for r in data['jobs'][0]['resource_waits']], ['pending'])

    def test_dashboard_escapes_resources_and_keeps_unknown_usage_explicit(self):
        data = snapshot()
        data['checked_at'] = '</script><script>alert(1)</script>'
        data['jobs'] = [dict(job_id=1, company='<img src=x onerror=evil()>', title='Engineer', counted=True,
                             owner='<owner>', submitted=False, synced=False, closed=False,
                             next_step='Wait', errors=[], turn_state='active')]
        attach_resources(data, dict(tabs=[dict(job_id=1, browser='iab', tab_id='<foreign>', opener_tab_id='parent', state='observed')],
                                    claims=[dict(job_id=1, kind='native', state='needs_reconciliation')]))
        html = render_dashboard(data)
        self.assertIn('&lt;img', html)
        self.assertIn('&lt;foreign&gt;', html)
        self.assertIn('popup of parent', html)
        self.assertIn('needs_reconciliation', html)
        self.assertIn('&lt;owner&gt;', html)
        self.assertNotIn('codex://', html)
        self.assertIn('用量未接入', html)
        self.assertIn('label for="filter"', html)
        self.assertNotIn('<script>alert(1)', html)
        self.assertIn(r'</script>', html)


if __name__ == '__main__':
    unittest.main()
