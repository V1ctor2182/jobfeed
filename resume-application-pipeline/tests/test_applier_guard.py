import json
from pathlib import Path
import subprocess
import sys
import unittest

GUARD = Path(__file__).resolve().parents[1] / 'pipeline' / 'applier_guard.py'


def run(event, env=None):
    return subprocess.run([sys.executable, '-I', str(GUARD)], input=json.dumps(event), capture_output=True, text=True, env=env)


class GuardTests(unittest.TestCase):
    def test_navigating_to_the_dashboard_is_blocked(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': 'http://127.0.0.1:8765/runs', 'tabId': 1}})
        self.assertEqual(result.returncode, 2)
        self.assertIn('never open the local Dashboard', result.stderr)

    def test_a_script_that_reaches_the_dashboard_is_blocked(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__javascript_tool', 'tool_input': {'text': "fetch('http://LOCALHOST:8765/api/application-submissions')"}})
        self.assertEqual(result.returncode, 2)

    def test_other_pages_pass(self):
        result = run({'tool_name': 'mcp__claude-in-chrome__navigate', 'tool_input': {'url': 'https://boards.greenhouse.io/acme/jobs/1'}})
        self.assertEqual(result.returncode, 0)

    def test_unreadable_input_is_blocked(self):
        result = subprocess.run([sys.executable, '-I', str(GUARD)], input='not json', capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
