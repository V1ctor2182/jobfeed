import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stdout

from pipeline.applier_discord import DiscordHTTPError
from pipeline import discord_setup

TOKEN = 'bot-token-secret'


class FakeDiscord:
    def __init__(self, flags=1 << 19, guilds=({'id': 'g1', 'name': 'Jobs'},), refuse=()):
        self.flags, self.guilds, self.refuse, self.calls = flags, list(guilds), set(refuse), []

    def client(self, token):
        def call(method, path, body=None, file=None):
            self.calls.append((method, path, body))
            if token != TOKEN:
                raise DiscordHTTPError('Discord GET failed with 401', 401)
            if (method, path.split('/')[1]) in self.refuse:
                raise DiscordHTTPError('Discord refused', 403)
            if path == '/users/@me':
                return {'id': 'b1', 'username': 'Jobfeed Appliers'}
            if path == '/applications/@me':
                return {'id': 'a1', 'flags': self.flags}
            if path == '/users/@me/guilds':
                return self.guilds
            if path.endswith('/channels'):
                return [{'id': 'voice', 'type': 2}, {'id': 'c1', 'type': 0}]
            if path.startswith('/guilds/'):
                return {'id': 'g1', 'owner_id': 'u1'}
            if path.endswith('/threads'):
                return {'id': 't1'}
            return {'id': 'm1'}
        return call


class DiscordSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / 'config/jobfeed-applier/discord.env'

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self, fake, *args, token=TOKEN):
        out = io.StringIO()
        with redirect_stdout(out):
            code = discord_setup.main(['--env', str(self.env), *args], client=fake.client, token_reader=lambda: token)
        return code, out.getvalue(), json.loads(out.getvalue())

    def test_a_working_bot_posts_one_setup_thread_and_saves_a_private_token_file(self):
        fake = FakeDiscord()
        code, text, result = self.run_setup(fake)
        self.assertEqual(code, 0)
        self.assertEqual((result['server'], result['channel_id']), ('Jobs', 'c1'))
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.env.read_text(), f'DISCORD_BOT_TOKEN={TOKEN}\nDISCORD_GUILD_ID=g1\n')
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.env.parent.stat().st_mode), 0o700)
        posts = [(m, p) for m, p, _ in fake.calls if m != 'GET']
        self.assertEqual(posts, [('POST', '/channels/c1/messages'), ('POST', '/channels/c1/messages/m1/threads'),
                                 ('POST', '/channels/t1/messages'), ('PATCH', '/channels/t1')])

    def test_check_posts_nothing(self):
        self.run_setup(FakeDiscord())
        fake = FakeDiscord()
        code, _, result = self.run_setup(fake, '--check')
        self.assertEqual((code, result['connected']), (0, True))
        self.assertEqual({m for m, _, _ in fake.calls}, {'GET'})

    def test_check_before_setup_says_so(self):
        code, _, result = self.run_setup(FakeDiscord(), '--check')
        self.assertEqual((code, result['problem']), (1, 'not_set_up'))

    def test_each_missing_setting_names_its_fix_and_saves_nothing(self):
        cases = [(FakeDiscord(), 'wrong-token', 'bad_token'),
                 (FakeDiscord(flags=0), TOKEN, 'message_content_off'),
                 (FakeDiscord(guilds=()), TOKEN, 'not_in_server'),
                 (FakeDiscord(guilds=({'id': 'g1', 'name': 'A'}, {'id': 'g2', 'name': 'B'})), TOKEN, 'several_servers'),
                 (FakeDiscord(refuse={('POST', 'channels')}), TOKEN, 'no_permission')]
        for fake, token, problem in cases:
            code, text, result = self.run_setup(fake, token=token)
            self.assertEqual((code, result['problem']), (1, problem))
            self.assertTrue(result['fix'])
            self.assertNotIn(TOKEN, text)
            self.assertFalse(self.env.exists())
            self.assertNotIn(TOKEN, (self.env.parent / 'discord-setup.json').read_text())
        _, _, result = self.run_setup(FakeDiscord(guilds=()))
        self.assertIn('client_id=a1', result['invite_url'])
        self.assertIn(f'permissions={discord_setup.PERMISSIONS}', result['invite_url'])

    def test_a_rejected_token_is_never_kept(self):
        self.run_setup(FakeDiscord(), token='wrong-token')
        self.assertFalse(self.env.with_name('discord.env.pending').exists())

    def test_after_adding_the_bot_the_setup_finishes_without_a_second_paste(self):
        code, _, result = self.run_setup(FakeDiscord(guilds=()))
        self.assertEqual((code, result['problem']), (1, 'not_in_server'))
        pending = self.env.with_name('discord.env.pending')
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
        code, _, result = self.run_setup(FakeDiscord(), '--again', token='')
        self.assertEqual((code, result['server_id']), (0, 'g1'))
        self.assertIn(TOKEN, self.env.read_text())
        self.assertFalse(pending.exists())
        self.assertTrue(json.loads((self.env.parent / 'discord-setup.json').read_text())['connected'])

    def test_a_named_server_picks_one_of_several(self):
        fake = FakeDiscord(guilds=({'id': 'g1', 'name': 'A'}, {'id': 'g2', 'name': 'B'}))
        code, _, result = self.run_setup(fake, '--server', 'g2')
        self.assertEqual((code, result['server_id']), (0, 'g2'))


if __name__ == '__main__':
    unittest.main()
