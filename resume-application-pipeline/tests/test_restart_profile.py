import json
import shlex
import unittest
from unittest.mock import patch

from pipeline.application_profile import ProfileBridge, candidates
from pipeline.application_rerun import RerunError
from pipeline.resume_catalog import verify_selection
from tests import test_application_restart as lifecycle


class RestartProfileTests(unittest.TestCase):
    setUp = lifecycle.ApplicationRestartTests.setUp
    request = lifecycle.ApplicationRestartTests.request
    profile = lifecycle.ApplicationRestartTests.profile
    pair = lifecycle.ApplicationRestartTests.pair
    prepare = lifecycle.ApplicationRestartTests.prepare

    def test_generated_profile_commands_keep_shell_owner_quoting(self):
        self.request()
        details = json.loads(self.restarts.row(self.ident)['details'])
        for prompt, operation in [(self.f.sent[0][1], 'verify-profile'),
                                  (details['create_arguments']['prompt'], 'verify-profile'),
                                  (details['create_arguments']['prompt'], 'pair-tab')]:
            command = self.restarts.command(self.ident, operation) + ' --owner "$CODEX_THREAD_ID"'
            self.assertIn(command + ' with ', prompt)
            self.assertEqual(shlex.split(command)[-2:], ['--owner', '$CODEX_THREAD_ID'])
            self.assertNotIn('\\"$CODEX_THREAD_ID\\"', prompt)

    def test_missing_extension_capability_never_queues_restart(self):
        self.bridge.available.side_effect = RerunError('Reload the Jobfeed extension.')
        with self.assertRaisesRegex(RerunError, 'Reload'):
            self.request()
        self.assertEqual(self.f.sent, [])
        self.assertEqual(self.f.resources.snapshot([1])['tabs'][0]['state'], 'observed')

    def test_wrong_cua_profile_keeps_original_form(self):
        self.bridge.probe.return_value = {'profile_proof': 'none', 'expires_at': 'later', 'matches': []}
        self.bridge.probe.side_effect = None
        with self.assertRaisesRegex(RerunError, 'No unique CUA profile'):
            self.request()
        row = self.restarts.row(self.ident)
        self.assertIsNone(json.loads(row['details'])['profile'])
        self.assertEqual(self.f.resources.snapshot([1])['tabs'][0]['state'], 'observed')
        self.bridge.validate.side_effect = RerunError('Verify the Jobfeed Chrome profile before closing any application tab.')
        with self.assertRaisesRegex(RerunError, 'profile'):
            self.restarts.prepare(row, 'owner')

    def test_expired_or_replaced_profile_connection_prevents_creation(self):
        self.request()
        self.bridge.validate.side_effect = RerunError('Profile connection expired or replaced.')
        with self.assertRaisesRegex(RerunError, 'expired or replaced'):
            self.restarts.prepare(self.restarts.row(self.ident), 'owner')
        self.assertEqual(self.restarts.row(self.ident)['phase'], 'relay_pending')
        self.assertEqual(self.f.resources.snapshot([1])['tabs'][0]['state'], 'observed')

    def replacement(self):
        prepared = self.prepare()
        self.f.replacement(prepared['arguments'])
        self.restarts.acknowledge(self.restarts.row(self.ident), 'owner', 'replacement')

    def test_replacement_cannot_switch_to_another_cua_profile(self):
        self.replacement()
        with self.assertRaisesRegex(RerunError, 'absent from the fresh CUA'):
            self.restarts.verify_profile(self.restarts.row(self.ident), 'replacement', {'candidates': [{
                'browser_id': 'wrong', 'extension_instance_id': 'unrelated-profile', 'profile_name': 'Personal',
                'native_tab_id': 99, 'url': self.f.job.url}]})
        with self.assertRaisesRegex(ValueError, 'live restart'):
            verify_selection(self.f.config, str(self.f.scope), 1, 'replacement', restart_request=self.ident, turn_id='replacement-turn')

    def test_replacement_can_reprove_same_profile_after_socket_reconnect(self):
        self.replacement()
        self.bridge.validate.side_effect = RerunError('Old proof expired.')
        result = self.profile('replacement')
        self.assertEqual(result['status'], 'profile_verified')
        self.assertEqual(result['extension_instance_id'], 'jobfeed-profile')
        self.bridge.probe.side_effect = None
        self.bridge.probe.return_value = {'profile_proof': 'other-socket', 'expires_at': 'later', 'matches': []}
        with self.assertRaisesRegex(RerunError, 'No unique CUA profile'):
            self.profile('replacement')

    def test_pairing_requires_exact_recorded_tab_and_connected_result(self):
        self.replacement()
        self.profile('replacement')
        with self.assertRaisesRegex(RerunError, 'Record this exact'):
            self.restarts.pair_tab(self.restarts.row(self.ident), 'replacement', {
                'browser_id': 'fresh-browser', 'native_tab_id': 999, 'url': self.f.job.url})
        self.bridge.connect.assert_not_called()
        self.bridge.connect.return_value['state'] = 'disconnected'
        with self.assertRaisesRegex(RerunError, 'not connected'):
            self.pair()
        with self.assertRaisesRegex(ValueError, 'live restart'):
            verify_selection(self.f.config, str(self.f.scope), 1, 'replacement', restart_request=self.ident, turn_id='replacement-turn')
        self.bridge.connect.return_value['state'] = 'connected'
        self.pair()
        self.assertEqual(verify_selection(self.f.config, str(self.f.scope), 1, 'replacement', restart_request=self.ident, turn_id='replacement-turn')['owner'], 'replacement')

    def test_ambiguous_native_id_across_profiles_is_rejected(self):
        self.request()
        self.bridge.probe.side_effect = lambda inventory: {'profile_proof': 'proof', 'expires_at': 'later',
            'matches': [{'native_tab_id': 11, 'url': self.f.job.url}]}
        with self.assertRaisesRegex(RerunError, 'No unique CUA profile'):
            self.restarts.verify_profile(self.restarts.row(self.ident), 'owner', {'candidates': [
                {'browser_id': name, 'extension_instance_id': name, 'profile_name': name,
                 'native_tab_id': 11, 'url': self.f.job.url} for name in ['first', 'second']]})


class ProfileBridgeBoundaryTests(unittest.TestCase):
    def test_bridge_requires_local_http_bound_server(self):
        for url in [None, 'http://evil.example:1234', 'http://127.0.0.1:1234/path', 'http://u:p@127.0.0.1:1234', 'https://jobfeed.localhost:1355']:
            with self.subTest(url=url), self.assertRaises(RerunError):
                ProfileBridge(url)
        self.assertEqual(ProfileBridge('http://127.0.0.1:4321').base_url, 'http://127.0.0.1:4321')

    def test_profile_candidates_are_bounded_and_require_stable_identity(self):
        candidate = {'browser_id': '1', 'native_tab_id': 12, 'url': 'https://example.com',
                     'extension_instance_id': 'stable', 'profile_name': 'Work'}
        self.assertEqual(candidates({'candidates': [candidate]}), [candidate])
        for values in [[], [candidate] * 17, [{**candidate, 'extension_instance_id': ''}], [{**candidate, 'native_tab_id': True}]]:
            with self.subTest(values=values), self.assertRaises(RerunError):
                candidates({'candidates': values})

    def test_capability_failure_explains_extension_reload(self):
        bridge = ProfileBridge('http://127.0.0.1:4321')
        with patch.object(bridge, 'request', return_value={'profile_probe_available': False,
                'profile_probe_reason': 'Reload Jobfeed Jobright Source in Chrome.'}):
            with self.assertRaisesRegex(RerunError, 'Reload Jobfeed'):
                bridge.available()
