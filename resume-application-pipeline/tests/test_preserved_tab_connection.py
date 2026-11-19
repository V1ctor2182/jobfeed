import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pipeline.application_profile import connect_recorded_tab, RerunError


class PreservedTabConnectionTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(resume_root='/fixture', jobfeed_db='/fixture/jobfeed.sqlite')
        self.target = {'browser_id': 'chrome-profile', 'native_tab_id': 123, 'url': 'https://ats.test/form?draft=1'}
        self.candidate = self.target | {'extension_instance_id': 'cua-extension', 'profile_name': 'Applicant'}
        self.snapshot = {'bindings': [{'owner': 'owner'}], 'tabs': [{'owner': 'owner', 'browser_id': 'chrome-profile',
                          'tab_id': '123', 'origin': 'https://ats.test', 'state': 'preserved'}]}
        store = patch('pipeline.coordination.CoordinationStore')
        self.store = store.start()
        self.addCleanup(store.stop)
        self.store.return_value.snapshot.return_value = self.snapshot
        bridge = patch('pipeline.application_profile.ProfileBridge')
        self.bridge = bridge.start().return_value
        self.addCleanup(bridge.stop)
        self.bridge.probe.return_value = {'matches': [{'native_tab_id': 123, 'url': self.target['url']}], 'profile_proof': 'fresh-proof'}
        self.bridge.connect.return_value = {'job_id': 1, 'owner_thread_id': 'owner', 'state': 'connected'}

    def connect(self, candidates=None):
        return connect_recorded_tab(self.config, '/fixture/batch.json', 1, 'owner',
                                    {'candidates': candidates or [self.candidate], 'target': self.target}, 'http://127.0.0.1:7654')

    def test_existing_exact_form_connects_with_fresh_profile_proof(self):
        self.assertEqual(self.connect()['state'], 'connected')
        self.bridge.connect.assert_called_once_with(1, 'owner', 'fresh-proof', self.target)

    def test_wrong_owner_or_missing_tab_never_probes_or_connects(self):
        for replacement in ({'bindings': [{'owner': 'other'}], 'tabs': self.snapshot['tabs']},
                            {'bindings': self.snapshot['bindings'], 'tabs': []}):
            self.store.return_value.snapshot.return_value = replacement
            with self.assertRaises(RerunError):
                self.connect()
        self.bridge.probe.assert_not_called()
        self.bridge.connect.assert_not_called()

    def test_same_tab_number_in_multiple_profiles_is_not_authority(self):
        with self.assertRaisesRegex(RerunError, 'uniquely'):
            self.connect([self.candidate, self.candidate | {'browser_id': 'foreign', 'extension_instance_id': 'other'}])
        self.bridge.connect.assert_not_called()

    def test_full_url_mismatch_does_not_connect(self):
        self.bridge.probe.return_value['matches'][0]['url'] = 'https://ats.test/form?draft=other'
        with self.assertRaises(RerunError):
            self.connect()
        self.bridge.connect.assert_not_called()
