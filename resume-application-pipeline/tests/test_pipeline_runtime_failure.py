import unittest
from unittest.mock import Mock

from pipeline.codex_app_server import CodexAppServer, AppServerError, AppServerTurnFailed


class RuntimeFailureTests(unittest.TestCase):
    def test_pipeline_disables_incompatible_hosted_search(self):
        self.assertIn('web_search="disabled"', CodexAppServer().command)

    def test_failed_latest_turn_cannot_return_old_success(self):
        server = CodexAppServer()
        server.request = Mock(return_value={'thread': {'turns': [
            {'status': 'completed', 'items': [{'type': 'agentMessage',
             'phase': 'final_answer', 'text': 'old success'}]},
            {'id': 'new', 'status': 'failed', 'error': {'message': 'unsupported tool'}, 'items': []},
        ]}})
        with self.assertRaisesRegex(AppServerTurnFailed, 'unsupported tool'):
            server.latest_final_answer('owner')

    def test_active_latest_turn_cannot_return_old_success(self):
        server = CodexAppServer()
        server.request = Mock(return_value={'thread': {'turns': [
            {'status': 'completed', 'items': [{'type': 'agentMessage',
             'phase': 'final_answer', 'text': 'old success'}]},
            {'status': 'inProgress', 'items': []},
        ]}})
        with self.assertRaisesRegex(AppServerError, 'inProgress'):
            server.latest_final_answer('owner')

