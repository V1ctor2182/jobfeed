import json
import os
from pathlib import Path
import socket
import tempfile
import unittest

from jobfeed.my_profile_document import PersonalProfileStore
from jobfeed.my_profile_models import PersonalDetails

from pipeline import applier_check
from pipeline.apply_browser import EXTENSION
from pipeline.browser_broker import socket_path
from test_discord_setup import TOKEN, FakeDiscord

DEVICE = '19c496b6-d4eb-489b-b1de-d3e28844653e'
RUNNING = [{'type': 'service_worker', 'url': f'chrome-extension://{EXTENSION}/service-worker.js'}]


class ApplierCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        top = Path(self.temp.name) / 'jobfeed-home'
        self.root = top / 'resume-application-pipeline'
        (self.root / '.pipeline').mkdir(parents=True)
        jobfeed = top / 'jobfeed'
        (jobfeed / '.venv/bin').mkdir(parents=True)
        (jobfeed / 'data').mkdir()
        (jobfeed / 'data/jobfeed.sqlite').write_bytes(b'')
        command = jobfeed / '.venv/bin/jobfeed'
        command.write_text('#!/bin/sh\n')
        command.chmod(0o755)
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        (self.bin / 'claude').write_text('#!/bin/sh\n')
        (self.bin / 'claude').chmod(0o755)
        self.claude_config = Path(self.temp.name) / 'claude.json'
        self.discord_env = Path(self.temp.name) / 'discord.env'
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        self.temp.cleanup()

    def set_up_everything(self):
        store = PersonalProfileStore(self.root / 'application-profile.md', self.root.parent / 'jobfeed/data/my-profile-review.json')
        store.save(PersonalDetails(legal_name='Sam Doe', primary_email='sam@example.com', phone='555-0100',
                                   work_authorization='US citizen'), store.state().revision)
        (self.root / 'resume.pdf').write_bytes(b'%PDF-1.4 synthetic resume')
        (self.root / 'resume-catalog.json').write_text(json.dumps({'schema_version': 1, 'resumes': [
            {'id': 'main', 'label': 'resume.pdf', 'pdf': 'resume.pdf', 'available': True}]}))
        self.claude_config.write_text(json.dumps({'projects': {str(self.root.parent): {'hasTrustDialogAccepted': True}}}))
        self.discord_env.write_text(f'DISCORD_BOT_TOKEN={TOKEN}\nDISCORD_GUILD_ID=g1\n')
        (self.root / '.pipeline/apply-browser.json').write_text(json.dumps({'device_id': DEVICE}))
        (self.root / '.pipeline/account-password').write_text('secret')
        with socket_path(self.root) as path:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen()
        self.servers.append(server)

    def results(self, targets=RUNNING):
        found = applier_check.checks(self.root, env={'PATH': str(self.bin)}, discord_env=self.discord_env,
                                     claude_config=self.claude_config, targets=lambda: targets,
                                     discord_client=FakeDiscord().client)
        return {r['name']: r for r in found}

    def test_a_complete_setup_passes_every_check(self):
        self.set_up_everything()
        results = self.results()
        self.assertEqual([name for name, r in results.items() if not r['ok']], [])
        self.assertEqual(results['Applying browser recorded']['detail'], DEVICE)

    def test_a_fresh_install_names_a_fix_for_each_missing_part(self):
        results = self.results(targets=[])
        failing = {name for name, r in results.items() if not r['ok']}
        self.assertEqual(failing, {'My Profile', 'Résumés for applications', 'Claude trusts this folder', 'Discord',
                                   'Applying browser', 'Applying browser recorded', 'Workday account program',
                                   'Job-site password'})
        self.assertTrue(all(results[name]['fix'] for name in failing))

    def test_a_profile_without_work_authorization_is_not_ready(self):
        self.set_up_everything()
        store = PersonalProfileStore(self.root / 'application-profile.md', self.root.parent / 'jobfeed/data/my-profile-review.json')
        store.save(PersonalDetails(legal_name='Sam Doe', primary_email='sam@example.com', phone='555-0100'), store.state().revision)
        self.assertIn('work_authorization', self.results()['My Profile']['fix'])

    def test_the_check_starts_and_posts_nothing(self):
        self.set_up_everything()
        fake = FakeDiscord()
        applier_check.checks(self.root, env={'PATH': str(self.bin)}, discord_env=self.discord_env, claude_config=self.claude_config,
                             targets=lambda: RUNNING, discord_client=fake.client)
        self.assertEqual({method for method, _, _ in fake.calls}, {'GET'})
        self.assertFalse((self.root / 'applications').exists())
        self.assertEqual(sorted(os.listdir(self.root / '.pipeline')), ['account-password', 'apply-browser.json', 'browser-broker.sock'])


if __name__ == '__main__':
    unittest.main()
