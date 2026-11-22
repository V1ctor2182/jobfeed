import json
from pathlib import Path
import subprocess
import sys
import unittest

GUARD = Path(__file__).resolve().parents[1] / 'pipeline' / 'applier_guard.py'


def run(event, env=None, args=()):
    return subprocess.run([sys.executable, '-I', str(GUARD), *args], input=json.dumps(event), capture_output=True, text=True, env=env)


class GuardTests(unittest.TestCase):
    def test_navigating_to_the_dashboard_is_blocked(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': 'http://127.0.0.1:8765/runs', 'tabId': 1}})
        self.assertEqual(result.returncode, 2)
        self.assertIn('never open the local Dashboard', result.stderr)

    def test_a_script_that_reaches_the_dashboard_is_blocked(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__javascript_tool', 'tool_input': {'text': "fetch('http://LOCALHOST:8765/api/application-submissions')"}})
        self.assertEqual(result.returncode, 2)

    def test_a_dashboard_on_another_port_is_blocked_by_every_loopback_name(self):
        # A server install serves the Dashboard on 7654, and the launcher passes its addresses.
        hosts = '127.0.0.1:7654,localhost:7654,[::1]:7654,0.0.0.0:7654'
        for url in ('http://localhost:7654/review', 'http://127.0.0.1:7654/', 'http://[::1]:7654/api/application-submissions'):
            result = run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': url}}, args=[hosts])
            self.assertEqual(result.returncode, 2, url)
        self.assertEqual(run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': 'http://127.0.0.1:8765/'}}, args=[hosts]).returncode, 2)

    def test_other_pages_pass(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': 'https://boards.greenhouse.io/acme/jobs/1'}})
        self.assertEqual(result.returncode, 0)

    def test_unreadable_input_is_blocked(self):
        result = subprocess.run([sys.executable, '-I', str(GUARD)], input='not json', capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
