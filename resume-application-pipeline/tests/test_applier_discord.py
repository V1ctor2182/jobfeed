from contextlib import closing
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from confirmation_fixture import write_confirmation
from pipeline.applier_discord import RECORDED_TEXT, DiscordBridge, confirmation_reader, http_client, question_message, review_messages
from pipeline.applier_runtime import ApplierRuntime
from pipeline.config import PipelineConfig
from pipeline.store import ApplicationQueue

ALLOWED, STRANGER, BOT = '400', '999', '111'


def text(message):
    """Everything a person reads in a message: its content and its embeds."""
    parts = [message.get('content') or '']
    for embed in message.get('embeds') or []:
        parts += [embed.get('title', ''), embed.get('description', '')]
        parts += [f"{f['name']}: {f['value']}" for f in embed.get('fields', [])]
        parts.append((embed.get('footer') or {}).get('text', ''))
    return '\n'.join(parts)


def buttons(message):
    return {b['label']: b['custom_id'] for row in message.get('components') or [] for b in row['components']}


class FakeDiscord:
    def __init__(self):
        self.messages, self.calls, self.callbacks, self.next_id = {}, [], [], 1000
        self.threads = {}  # thread id -> {'name', 'archived'}

    def new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def say(self, channel, author, content, reply_to=None, bot=False, embeds=None, components=None):
        message = {'id': self.new_id(), 'author': {'id': author, 'bot': bot}, 'content': content, 'embeds': embeds or [],
                   'components': components or [], 'message_reference': {'message_id': reply_to} if reply_to else None}
        self.messages.setdefault(channel, []).append(message)
        return message

    def __call__(self, method, path, body=None, file=None):
        self.calls.append((method, path, body))
        parts = path.split('?')[0].strip('/').split('/')
        if method == 'POST' and parts[0] == 'interactions':
            self.callbacks.append(body)
            return None
        if method == 'POST' and parts[-1] == 'messages':
            if parts[1] in self.threads:
                self.threads[parts[1]]['archived'] = False  # A new message reopens an archived thread.
            reply = (body.get('message_reference') or {}).get('message_id')
            message = self.say(parts[1], BOT, body.get('content', ''), reply, bot=True, embeds=body.get('embeds'), components=body.get('components'))
            if file is not None:
                message['attachments'] = [dict(zip(('filename', 'content_type', 'data'), file))]
            return message
        if method == 'POST' and parts[-1] == 'threads':
            thread = self.new_id()
            self.messages[thread] = []
            self.threads[thread] = {'name': body['name'], 'archived': False}
            return {'id': thread}
        if method == 'PATCH' and parts[0] == 'channels' and len(parts) == 2:
            self.threads[parts[1]].update(body)
            return self.threads[parts[1]]
        if method == 'PATCH' and parts[0] == 'channels':
            if self.threads.get(parts[1], {}).get('archived'):
                from pipeline.applier_discord import DiscordHTTPError
                raise DiscordHTTPError(f'Discord PATCH {path} failed with 400', 400)  # Discord: the thread is archived.
            message = next(m for m in self.messages[parts[1]] if m['id'] == parts[3])
            message.update(body)
            return message
        if method == 'PATCH' and parts[0] == 'webhooks':
            self.callbacks.append({'edit': body})
            return None
        if method == 'GET' and parts[-1] == 'messages':
            after = int(path.split('after=')[1]) if 'after=' in path else 0
            return [m for m in self.messages.get(parts[1], []) if int(m['id']) > after and not m.get('interaction_only')]
        if method == 'PUT':
            return None
        raise AssertionError((method, path))


class DiscordBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.store.bind(1, 'owner-1', 'batch', 'opus')
        self.discord = FakeDiscord()
        self.bridge = DiscordBridge(self.root, http=self.discord, channel_id='general', allowed_user_ids=[ALLOWED],
                                    job_urls=lambda job_id: 'https://example.test/job')

    def thread(self):
        return self.bridge.thread_for(1)

    def bot_posts(self):
        return [m for m in self.discord.messages[self.thread()] if m['author']['bot']]

    def asked(self):
        """The questions and blockers posted in the main channel. Thread starters there carry no embed."""
        return [m for m in self.discord.messages.get('general', []) if m['author']['bot'] and m['embeds']]

    def asked_in_thread(self, request):
        """A question as an older version posted it in its job's thread, with its buttons."""
        message = question_message(self.store.request(request), 'https://example.test/job', 'Job 1')
        post = self.discord.say(self.thread(), BOT, message['content'], bot=True, embeds=message['embeds'], components=message['components'])
        self.bridge._mark('request', request, post['id'])
        self.bridge._mark('request-styled', request, post['id'])
        return post

    def ask(self, text='What is your start date?', **kwargs):
        return self.store.ask(1, 'owner-1', 'question', text, field='start_date', **kwargs)

    def press(self, message, label, user=ALLOWED, form=None):
        """Press a button, or submit the form it opened, as Discord's gateway delivers it."""
        custom_id = buttons(message)[label]
        interaction = {'id': self.discord.new_id(), 'token': 'tok', 'application_id': 'app', 'type': 3,
                       'data': {'custom_id': custom_id}, 'member': {'user': {'id': user}}, 'message': message}
        if form is not None:
            interaction.update(type=5, data={'custom_id': custom_id, 'components': [{'type': 1, 'components': [{'custom_id': 'text', 'value': form}]}]})
        self.bridge.interact(interaction)
        return self.discord.callbacks[-1] if 'edit' not in self.discord.callbacks[-1] else self.discord.callbacks[-2]

    def test_a_question_is_posted_once_in_the_main_channel_with_other_and_ignore_buttons(self):
        self.ask()
        self.bridge.sync()
        self.bridge.sync()
        posts = self.asked()
        self.assertEqual(len(posts), 1)
        self.assertIn('What is your start date?', text(posts[0]))
        self.assertIn('Form field: start_date', text(posts[0]))
        self.assertEqual(posts[0]['embeds'][0]['url'], 'https://example.test/job')
        self.assertEqual(list(buttons(posts[0])), ['Other', 'Ignore job'])

    def test_a_question_is_asked_in_the_main_channel_even_when_its_job_has_a_thread(self):
        thread = self.thread()
        self.ask()
        self.bridge.sync()
        self.assertEqual(sum('What is your start date?' in text(m) for m in self.asked()), 1)
        self.assertFalse(any('What is your start date?' in text(m) for m in self.discord.messages[thread]))

    def test_a_job_whose_applier_has_stopped_is_not_asked_and_its_earlier_question_is_closed(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT, job_id INTEGER, state TEXT)')
            db.execute("INSERT INTO applier_launches VALUES('l1', 1, 'running')")
        self.ask()
        self.bridge.sync()
        asked = self.asked()[0]
        self.assertEqual(list(buttons(asked))[-1], 'Ignore job')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE applier_launches SET state='stopped'")
        self.ask('Do you need sponsorship?', options=['Yes', 'No'])
        self.bridge.sync()
        self.assertEqual(len(self.asked()), 1, 'a stopped Applier cannot take an answer')
        self.assertEqual(asked['components'], [])
        self.assertIn('nothing is waiting for this answer', asked['content'])

    def test_the_question_of_a_parked_applier_keeps_its_buttons(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT, job_id INTEGER, state TEXT)')
            db.execute("INSERT INTO applier_launches VALUES('l1', 1, 'parked')")
        self.ask()
        self.bridge.sync()
        self.ask('Do you need sponsorship?', options=['Yes', 'No'])
        self.bridge.sync()
        self.assertEqual([list(buttons(m))[-1] for m in self.asked()], ['Ignore job', 'Ignore job'])

    def test_an_answer_to_a_question_in_an_archived_thread_settles_once_without_failing_the_pass(self):
        # 2026-11-18: a nine-day-old question in an archived thread failed with 400 on every pass, ending the rest of it.
        request = self.ask()
        post = self.asked_in_thread(request)
        self.discord.threads[self.thread()] = {'name': 'Job 1', 'archived': True}
        self.store.reply(request, 'June 1', source_kind='dashboard', source_id='click-1', sender_id='400')
        self.bridge.sync()
        self.assertEqual(post['components'] and True, True, 'the archived message keeps its buttons')
        calls = len(self.discord.calls)
        self.bridge.sync()
        self.assertFalse([c for c in self.discord.calls[calls:] if c[0] == 'PATCH' and post['id'] in c[1]], 'and is not tried again')

    def test_a_question_asked_in_its_thread_before_is_asked_once_in_the_main_channel_and_either_answer_settles_both(self):
        start = self.ask()
        sponsorship = self.ask('Do you need sponsorship?', options=['Yes', 'No'])
        in_thread = {request: self.asked_in_thread(request) for request in (start, sponsorship)}
        self.bridge.sync()
        self.bridge.sync()
        in_channel = {request: next(m for m in self.asked() if self.store.request(request)['text'] in text(m)) for request in (start, sponsorship)}
        self.assertEqual(len(self.asked()), 2)
        self.assertEqual(list(buttons(in_channel[sponsorship])), ['Yes', 'No', 'Other', 'Ignore job'])

        self.press(in_channel[start], 'Other', form='June 1')
        self.press(in_thread[sponsorship], 'No')
        self.assertEqual(self.store.latest_reply(start)['text'], 'June 1')
        self.assertEqual(self.store.latest_reply(sponsorship)['text'], 'No')
        self.bridge.sync()
        self.assertEqual(in_thread[start]['components'], [])
        self.assertIn('Answered: June 1', in_thread[start]['content'])
        self.assertEqual((in_channel[sponsorship]['content'], in_channel[sponsorship]['components']), ('Job 1: Answered: No', []))
        self.assertEqual(len(self.asked()), 2)

    def test_a_question_posted_in_its_thread_before_buttons_is_asked_with_buttons_in_the_main_channel(self):
        request = self.ask()
        old = self.discord.say(self.thread(), BOT, 'Question: What is your start date?', bot=True)
        self.bridge._mark('request', request, old['id'])
        self.bridge.sync()
        self.assertEqual(len(self.bot_posts()), 1)
        self.assertEqual([list(buttons(m)) for m in self.asked()], [['Other', 'Ignore job']])

    def test_each_option_is_a_button_that_saves_exactly_that_choice(self):
        choices = ['Yes', 'No', 'Yes, but will require sponsorship in the future']
        request = self.ask('Are you legally eligible to work in the job location?', options=choices)
        self.bridge.sync()
        post = self.asked()[0]
        self.assertEqual(list(buttons(post)), [*choices, 'Other', 'Ignore job'])
        self.assertEqual([len(row['components']) for row in post['components']], [3, 2])
        self.assertEqual(self.press(post, 'No', user=STRANGER)['data']['flags'], 64)
        self.assertIsNone(self.store.latest_reply(request))
        update = self.press(post, 'Yes, but will require sponsorship in the future')
        self.assertEqual(update, {'type': 7, 'data': {'content': 'Job 1: Answered: Yes, but will require sponsorship in the future', 'components': []}})
        self.assertEqual(self.store.latest_reply(request)['text'], 'Yes, but will require sponsorship in the future')
        self.assertIn('already closed', self.press(post, 'No')['data']['content'])

    def test_many_options_fill_rows_of_five_above_the_other_buttons(self):
        self.ask('Pick a state', options=[f'State {n}' for n in range(12)])
        self.bridge.sync()
        rows = self.asked()[0]['components']
        self.assertEqual([len(row['components']) for row in rows], [5, 5, 2, 2])
        self.assertEqual([b['label'] for b in rows[-1]['components']], ['Other', 'Ignore job'])

    def test_only_the_allowed_account_can_answer(self):
        request = self.ask()
        self.bridge.sync()
        thread = self.thread()
        self.discord.say(thread, STRANGER, 'January 1')
        self.discord.say(thread, BOT, 'bot echo', bot=True)
        self.assertEqual(self.bridge.sync(), [])
        self.assertIsNone(self.store.latest_reply(request))
        self.assertEqual(self.press(self.asked()[0], 'Other', user=STRANGER)['data']['flags'], 64)
        self.assertIsNone(self.store.latest_reply(request))
        answer = self.discord.say(thread, ALLOWED, 'June 1')
        events = self.bridge.sync()
        self.assertEqual(events, [{'event': 'reply_saved', 'request_id': request, 'version': 1}])
        saved = self.store._read('SELECT text,source_id,sender_id FROM applier_replies', ())
        self.assertEqual(saved, [{'text': 'June 1', 'source_id': answer['id'], 'sender_id': ALLOWED}])

    def test_other_opens_a_form_and_the_form_saves_the_reply_and_settles_the_buttons(self):
        request = self.ask()
        self.bridge.sync()
        post = self.asked()[0]
        modal = self.press(post, 'Other')
        self.assertEqual((modal['type'], modal['data']['custom_id']), (9, f'answer:{request}'))
        update = self.press(post, 'Other', form='June 1')
        self.assertEqual(update, {'type': 7, 'data': {'content': 'Job 1: Answered: June 1', 'components': []}})
        self.assertEqual(self.store.latest_reply(request)['text'], 'June 1')
        saved = self.store._read('SELECT sender_id,source_kind FROM applier_replies', ())
        self.assertEqual(saved, [{'sender_id': ALLOWED, 'source_kind': 'discord'}])
        self.bridge.sync()
        self.assertFalse([c for c in self.discord.calls if c[0] == 'PATCH'], 'the press already settled the message')
        self.assertIn('already closed', self.press(post, 'Other')['data']['content'])

    def test_a_plain_reply_in_the_jobs_thread_also_takes_the_buttons_off(self):
        self.ask()
        self.bridge.sync()
        self.discord.say(self.thread(), ALLOWED, 'June 1')
        self.bridge.sync()
        post = self.asked()[0]
        self.assertEqual((post['content'], post['components']), ('Job 1: Answered: June 1', []))

    def test_with_two_open_questions_a_plain_reply_answers_neither_and_the_buttons_pick_one(self):
        first = self.ask('What is your start date?')
        second = self.ask('Do you need sponsorship?', options=['Yes', 'No'])
        self.bridge.sync()
        thread = self.thread()
        self.discord.say(thread, ALLOWED, 'No')
        self.bridge.sync()
        self.assertIsNone(self.store.latest_reply(second))
        self.assertTrue(any('Use the buttons on the one you are answering' in m['content'] for m in self.discord.messages[thread]))
        self.press(next(m for m in self.asked() if 'sponsorship' in text(m)), 'No')
        self.assertEqual(self.store.latest_reply(second)['text'], 'No')
        self.assertIsNone(self.store.latest_reply(first))

    def test_a_requeued_jobs_old_question_closes_and_an_answer_to_it_never_reaches_the_new_applier(self):
        old = self.ask()
        self.bridge.sync()
        old_post = self.asked()[0]
        old_buttons = {'components': old_post['components']}
        self.store.requeue(1, 'Applying', 'folder-1', 'owner-1', [], actor='user via pipeline.cli requeue', reason='outage')
        self.store.bind(1, 'owner-2', 'new-batch', 'opus')
        new = self.store.ask(1, 'owner-2', 'question', 'Do you need sponsorship?', field='sponsorship')
        self.bridge.sync()
        self.assertEqual((old_post['content'], old_post['components']),
                         ('Job 1: Closed: this job went back to the queue. Its new Applier asks again if it needs this.', []))
        self.assertIn('already closed', self.press(old_buttons, 'Other', form='June 1')['data']['content'])
        self.bridge.sync()
        self.assertIsNone(self.store.latest_reply(new))
        self.assertIsNone(self.store.latest_reply(old))

    def test_a_reply_to_an_old_question_in_its_thread_never_reaches_the_new_applier(self):
        old = self.ask()
        old_post = self.asked_in_thread(old)
        self.store.requeue(1, 'Applying', 'folder-1', 'owner-1', [], actor='user via pipeline.cli requeue', reason='outage')
        self.store.bind(1, 'owner-2', 'new-batch', 'opus')
        new = self.store.ask(1, 'owner-2', 'question', 'Do you need sponsorship?', field='sponsorship')
        self.bridge.sync()
        self.discord.say(self.thread(), ALLOWED, 'June 1', reply_to=old_post['id'])
        self.bridge.sync()
        self.assertIsNone(self.store.latest_reply(new))
        self.assertTrue(any('That question is closed' in m['content'] for m in self.bot_posts()))

    def test_alerts_and_readbacks_are_posted_once(self):
        request = self.ask()
        self.bridge.sync()
        self.discord.say(self.thread(), ALLOWED, 'June 1')
        self.bridge.sync()
        self.store.readback(1, 'owner-1', 'start_date', 'June 1', request_id=request, reply_version=1)
        self.store.alert(1, 'stalled', 'no progress since noon')
        self.bridge.sync()
        self.bridge.sync()
        contents = [m['content'] for m in self.bot_posts()]
        self.assertEqual(sum('Filled start_date: June 1 (reply version 1)' in c for c in contents), 1)
        self.assertEqual(sum(c.startswith('Watchdog: stalled.') for c in contents), 1)
        self.assertEqual(self.store.alerts(unsent=True), [])

    def test_the_next_question_comes_after_the_confirmation_of_the_last_answer(self):
        first = self.ask()
        self.bridge.sync()
        self.discord.say(self.thread(), ALLOWED, 'June 1')
        self.bridge.sync()
        self.store.readback(1, 'owner-1', 'start_date', 'June 1', request_id=first, reply_version=1)
        self.ask('Do you require sponsorship?')
        self.bridge.sync()
        filled = next(m for m in self.bot_posts() if m['content'].startswith('Filled start_date: June 1'))
        question = next(m for m in self.asked() if 'Do you require sponsorship?' in text(m))
        self.assertLess(int(filled['id']), int(question['id']))

    def ready(self, *fields):
        for field, value in fields:
            self.store.readback(1, 'owner-1', field, value)
        self.store.progress(1, 'owner-1', 'ready for review')
        self.bridge.sync()
        return [m for m in self.bot_posts() if m['components'] and 'Submit' in buttons(m)][-1]

    def bot_replies_to(self, message):
        return [m['content'] for m in self.bot_posts() if (m['message_reference'] or {}).get('message_id') == message['id']]

    def reactions(self, message):
        return [path.rsplit('/', 2)[-2] for method, path, _ in self.discord.calls
                if method == 'PUT' and f"/messages/{message['id']}/reactions/" in path]

    def test_every_reply_the_bot_reads_gets_a_reaction(self):
        self.bridge.confirm_submit = lambda *args: {'state': 'queued', 'reason': None}
        self.ask()
        self.bridge.sync()
        answer = self.discord.say(self.thread(), ALLOWED, 'June 1')
        self.bridge.sync()
        self.assertEqual(self.reactions(answer), ['%E2%9C%85'])
        summary = self.ready(('Sponsorship', 'Yes'))
        change = self.discord.say(self.thread(), ALLOWED, 'Sponsorship should be No', reply_to=summary['id'])
        self.bridge.sync()
        self.assertEqual(self.reactions(change), ['%E2%9C%85'])
        newest = self.ready(('Sponsorship', 'No'))
        unclear = self.discord.say(self.thread(), ALLOWED, 'what now?')
        approval = self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=newest['id'])
        stranger = self.discord.say(self.thread(), STRANGER, 'submit', reply_to=newest['id'])
        self.bridge.sync()
        self.assertEqual(self.reactions(approval), ['%E2%9C%85'])
        self.assertEqual(self.reactions(unclear), ['%F0%9F%91%80'])
        self.assertEqual(self.reactions(stranger), [])

    def test_a_ready_draft_is_summarized_and_submit_needs_a_reply_to_the_newest_summary(self):
        confirmed = []
        self.bridge.confirm_submit = lambda *args: confirmed.append(args) or {'state': 'queued', 'reason': None}
        self.store.record_resume(1, 'owner-1', 'swe', 'Software Engineer', 'resume-alexdoe.pdf')
        first = self.ready(('Full Name', 'Alex Doe'), ('Sponsorship', 'Yes'))
        self.bridge.sync()
        self.assertIn('**Resume:** Software Engineer, uploaded as `resume-alexdoe.pdf`', text(first))
        self.assertIn('Full Name: Alex Doe\nSponsorship: Yes', text(first))
        self.assertEqual(list(buttons(first)), ['Submit', 'Request a change', 'Ignore job'])
        self.assertEqual(sum('Ready for review' in text(m) for m in self.bot_posts()), 1)
        bare = self.discord.say(self.thread(), ALLOWED, 'submit')
        self.bridge.sync()
        self.assertIn('press Submit on the review summary', self.bot_replies_to(bare)[0])
        newest = self.ready(('Sponsorship', 'No'))
        self.assertIn('Full Name: Alex Doe\nSponsorship: No', text(newest))
        self.assertEqual((first['content'], first['components']), ('Out of date. Use the newest summary below.', []))
        stale = self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=first['id'])
        self.discord.say(self.thread(), STRANGER, 'submit', reply_to=newest['id'])
        self.bridge.sync()
        self.assertIn('out of date', self.bot_replies_to(stale)[0])
        self.assertEqual(confirmed, [])
        approval = self.discord.say(self.thread(), ALLOWED, 'Submit', reply_to=newest['id'])
        self.bridge.sync()
        self.bridge.sync()
        self.assertEqual(confirmed, [(1, 'batch', approval['id'], ALLOWED)])
        self.assertIn('Submit approved', self.bot_replies_to(approval)[0])

    def test_the_summary_lists_in_full_the_answers_the_applier_decided_for_this_job(self):
        from pipeline.applier_mcp import ApplierTools
        self.store.set_auto_submit(True, actor='user via test', source='test')
        why = 'I use Sierra agents at work and want to build them. ' * 8
        ApplierTools(self.store, 1, 'owner-1', company='Sierra').record_answer('Why are you excited to join us at Sierra?', why, 'this_company')
        self.store.bind(2, 'owner-2', 'batch', 'opus')
        ApplierTools(self.store, 2, 'owner-2', company='Other Co').record_answer('Any certifications?', 'N/A', 'every_company')
        self.ready(('Full Name', 'Alex Doe'))
        summary = '\n'.join(text(m) for m in self.bot_posts())
        self.assertIn('The Applier decided **1** answers itself under auto submit mode, without asking you.', summary)
        self.assertIn(f'Applier decided · Why are you excited to join us at Sierra?: {why}', summary)
        self.assertNotIn('Any certifications?', summary)

    def test_the_submit_button_asks_once_more_then_confirms_once(self):
        confirmed = []
        self.bridge.confirm_submit = lambda *args: confirmed.append(args) or {'state': 'queued', 'reason': None}
        summary = self.ready(('Sponsorship', 'No'))
        ask = self.press(summary, 'Submit')
        self.assertEqual(ask['data']['flags'], 64)
        self.assertIn('Submit job 1 once', ask['data']['content'])
        self.assertEqual(confirmed, [])
        confirmation = {'components': ask['data']['components']}
        self.assertEqual(self.press(confirmation, 'Yes, submit once'), {'type': 7, 'data': {'content': 'Confirming Submit…', 'components': []}})
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0][3], ALLOWED)
        self.assertIn('Submit approved', self.discord.callbacks[-1]['edit']['content'])
        self.assertIn('Submit approved', self.bot_replies_to(summary)[0])
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, 'owner-1')
        self.bridge.sync()
        self.assertEqual((summary['content'], summary['components']), ('Submit approved for this summary.', []))
        self.assertIn('out of date', self.press(confirmation, 'Yes, submit once')['data']['content'])
        self.assertEqual(len(confirmed), 1)

    def test_an_old_summary_button_is_refused(self):
        self.bridge.confirm_submit = lambda *args: {'state': 'queued', 'reason': None}
        old = self.ready(('Sponsorship', 'Yes'))
        old_buttons = {'components': old['components']}
        self.ready(('Sponsorship', 'No'))
        self.assertIn('out of date', self.press(old_buttons, 'Submit')['data']['content'])

    def test_a_change_goes_to_the_applier_and_holds_submit_until_the_new_summary(self):
        confirmed = []
        self.bridge.confirm_submit = lambda *args: confirmed.append(args) or {'state': 'queued', 'reason': None}
        summary = self.ready(('Sponsorship', 'Yes'))
        self.discord.say(self.thread(), ALLOWED, 'Sponsorship should be No', reply_to=summary['id'])
        self.bridge.sync()
        message = self.store.pending_messages()[0]
        self.assertIn('"Sponsorship should be No"', message['text'])
        self.assertIn('Do not submit', message['text'])
        self.assertEqual(summary['components'], [])
        early = self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=summary['id'])
        self.bridge.sync()
        self.assertIn('still being made', self.bot_replies_to(early)[0])
        self.assertEqual(confirmed, [])
        newest = self.ready(('Sponsorship', 'No'))
        self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=newest['id'])
        self.bridge.sync()
        self.assertEqual(len(confirmed), 1)

    def test_auto_submit_confirms_the_newest_summary_and_says_so_once_in_its_thread(self):
        calls = []
        self.bridge.confirm_auto = lambda *args: calls.append(args) or {'state': 'queued', 'reason': None, 'request_id': 'auto-1'}
        summary = self.ready(('Sponsorship', 'No'))
        self.bridge.sync()
        self.assertEqual(calls[0], (1, 'batch'))
        self.assertIn('Ready for review', text(summary))
        replies = self.bot_replies_to(summary)
        self.assertEqual(len(replies), 1)
        self.assertIn('Auto submit is on, so Submit was approved for this summary', replies[0])

    def test_auto_submit_posts_nothing_while_the_job_needs_the_user_and_says_why_it_was_blocked(self):
        self.bridge.confirm_auto = lambda *args: None
        summary = self.ready(('Sponsorship', 'No'))
        self.bridge.sync()
        self.assertEqual(self.bot_replies_to(summary), [])
        self.bridge.confirm_auto = lambda *args: {'state': 'blocked', 'reason': 'Preview changed.', 'request_id': 'auto-1'}
        self.bridge.sync()
        self.assertEqual(self.bot_replies_to(summary), ['Auto submit could not approve this summary: Preview changed. Check it and press Submit yourself.'])

    def test_auto_submit_never_confirms_a_summary_the_user_asked_to_change(self):
        calls = []
        self.bridge.confirm_auto = lambda *args: calls.append(args)
        summary = self.ready(('Sponsorship', 'Yes'))
        calls.clear()
        self.discord.say(self.thread(), ALLOWED, 'Sponsorship should be No', reply_to=summary['id'])
        self.bridge.sync()
        self.assertEqual(calls, [])
        self.ready(('Sponsorship', 'No'))
        self.assertEqual(calls, [(1, 'batch')])

    def test_request_a_change_opens_a_form_and_sends_it_to_the_applier(self):
        summary = self.ready(('Sponsorship', 'Yes'))
        self.assertEqual(self.press(summary, 'Request a change')['type'], 9)
        update = self.press(summary, 'Request a change', form='Sponsorship should be No')
        self.assertEqual(update['type'], 7)
        self.assertIn('Change requested: Sponsorship should be No', update['data']['content'])
        self.assertIn('"Sponsorship should be No"', self.store.pending_messages()[0]['text'])
        self.assertIn('still being made', self.press(summary, 'Submit')['data']['content'])

    def test_a_refused_confirmation_says_why_and_the_outcome_is_posted_once(self):
        self.bridge.confirm_submit = lambda *args: (_ for _ in ()).throw(ValueError('This job needs the Dashboard confirmation.'))
        summary = self.ready(('Sponsorship', 'Yes'))
        reply = self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=summary['id'])
        self.bridge.sync()
        self.assertEqual(self.bot_replies_to(reply), ['Not submitted. This job needs the Dashboard confirmation.'])
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, 'owner-1')
        self.store.record_submit_outcome(1, 'owner-1', 'not_submitted', 'Missing entry for required field: LinkedIn Profile')
        self.bridge.sync()
        self.bridge.sync()
        contents = [m['content'] for m in self.discord.messages[self.thread()]]
        outcome = [c for c in contents if c.startswith('Not submitted. The form is still open')]
        self.assertEqual(len(outcome), 1)
        self.assertIn('The Applier saw: Missing entry for required field: LinkedIn Profile', outcome[0])
        fixed = self.ready(('LinkedIn Profile', 'https://linkedin.example/in/v'))
        self.assertNotEqual(fixed['id'], summary['id'])
        self.assertIn('LinkedIn Profile', text(fixed))

    def test_a_retry_after_an_attempt_that_sent_nothing_gets_a_new_summary_even_without_new_readbacks(self):
        summary = self.ready(('Sponsorship', 'No'))
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, 'owner-1')
        self.bridge.sync()
        self.store.record_submit_outcome(1, 'owner-1', 'not_submitted', 'This field is required under Locations')
        self.bridge.sync()
        self.assertEqual([m for m in self.bot_posts() if m['components'] and 'Submit' in buttons(m)], [],
                         'no summary to approve until the Applier says the fixed form is ready')
        retry = self.ready()
        self.assertNotEqual(retry['id'], summary['id'])
        self.discord.say(self.thread(), ALLOWED, 'submit', reply_to=retry['id'])
        confirmed = []
        self.bridge.confirm_submit = lambda *args: confirmed.append(args) or {'state': 'queued'}
        self.bridge.sync()
        self.assertEqual(len(confirmed), 1)

    def test_a_submission_reports_the_closed_tab_and_the_jobfeed_record_once_each(self):
        self.store.register_tab(1, 'owner-1', 5, 'https://example.test/job')
        self.ready(('Sponsorship', 'No'))
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, 'owner-1')
        with self.assertRaisesRegex(ValueError, 'only when a tool result tells you to'):
            self.store.close_tabs(1, 'owner-1')
        self.store.record_submit_outcome(1, 'owner-1', 'submitted', 'Thanks for applying')
        self.assertEqual(self.store.close_tabs(1, 'owner-1'), [{'tab_id': '5', 'url': 'https://example.test/job'}])
        self.store.outbox_result(1)
        self.bridge.sync()
        self.bridge.sync()
        contents = [m['content'] for m in self.bot_posts()]
        for line in ('Submitted. The confirmation page was saved.', "Closed this job's browser tabs.", 'Recorded in Jobfeed as applied.'):
            self.assertEqual(contents.count(line), 1, line)

    def submit(self):
        self.store.register_tab(1, 'owner-1', 5, 'https://example.test/job')
        self.ready(('Sponsorship', 'No'))
        self.store.authorize_submit(1, source_kind='test', event_id='e1')
        self.store.request_submit(1, 'owner-1')
        self.store.record_submit_outcome(1, 'owner-1', 'submitted', 'Thanks for applying')

    def test_threads_and_messages_name_the_job(self):
        self.bridge.job_names = lambda job_id: 'Acme · Platform Engineer'
        self.ask()
        self.bridge.sync()
        question = self.asked()[0]
        thread = self.thread()
        self.assertEqual(self.discord.threads[thread]['name'], 'Acme · Platform Engineer (job 1)')
        starter = next(m for m in self.discord.messages['general'] if not m['embeds'])
        self.assertIn('Acme · Platform Engineer (job 1)', starter['content'])
        self.assertIn('Acme · Platform Engineer (job 1)', question['content'])
        self.assertIn('Acme · Platform Engineer (job 1) · Question', question['embeds'][0]['title'])
        summary = review_messages(1, '', [('Name', 'V')], label='Acme · Platform Engineer (job 1)')[0]
        self.assertIn('Acme · Platform Engineer (job 1) · Ready for review', summary['embeds'][0]['title'])

    def test_a_thread_made_before_names_is_renamed_once(self):
        self.ask()
        self.bridge.sync()
        thread = self.thread()
        self.assertEqual(self.discord.threads[thread]['name'], 'Job 1')
        self.bridge.job_names = lambda job_id: 'Acme · Platform Engineer'
        self.bridge.sync()
        self.bridge.sync()
        renames = [c for c in self.discord.calls if c[:2] == ('PATCH', f'/channels/{thread}')]
        self.assertEqual(renames, [('PATCH', f'/channels/{thread}', {'name': 'Acme · Platform Engineer (job 1)'})])

    def test_a_submitted_jobs_thread_is_archived_and_stays_archived_after_later_notes(self):
        self.submit()
        self.bridge.sync()
        thread = self.thread()
        self.assertTrue(self.discord.threads[thread]['archived'])
        self.store.close_tabs(1, 'owner-1')
        self.store.outbox_result(1)
        self.bridge.sync()
        contents = [m['content'] for m in self.bot_posts()]
        self.assertIn('Recorded in Jobfeed as applied.', contents)
        self.assertTrue(self.discord.threads[thread]['archived'])
        self.bridge.sync()
        archives = [c for c in self.discord.calls if c == ('PATCH', f'/channels/{thread}', {'archived': True})]
        self.assertEqual(len(archives), 3, 'once on submission, then after each of the two later notes')

    def test_an_unsubmitted_jobs_thread_stays_open(self):
        self.ask()
        self.bridge.sync()
        self.assertFalse(self.discord.threads[self.thread()]['archived'])

    def test_fixer_summaries_go_to_one_fixer_thread_once(self):
        from pipeline.applier_fixer import Fixer
        fixer = Fixer(self.root)
        with fixer._db() as db:
            db.execute("INSERT INTO applier_fix_runs(id,started_at,state,items,summary) VALUES('r1','now','committed','[]','Fixer: 1 fixed, 0 skipped.')")
        self.bridge.sync()
        self.bridge.sync()
        fixer_thread = self.bridge.thread_for(0)
        posts = [m['content'] for m in self.discord.messages[fixer_thread] if m['author']['bot']]
        self.assertEqual(posts, ['Fixer: 1 fixed, 0 skipped.'])
        self.assertEqual(fixer.unposted(), [])

    def test_a_plain_approve_saves_the_draft_and_any_other_plain_reply_asks_for_changes(self):
        draft = 'I build reliable backend systems.\nValon\'s mortgage work fits that.'
        drafted = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft=draft)
        self.bridge.sync()
        thread = self.thread()
        post = next(m for m in self.asked() if 'Why Valon?' in text(m))
        self.assertIn("> I build reliable backend systems.\n> Valon's mortgage work fits that.", text(post))
        self.assertEqual(list(buttons(post)), ['Approve draft', 'Request changes', 'Write my own', 'Ignore job'])
        self.assertIn('Write my own replaces the draft', text(post))
        approval = self.discord.say(thread, ALLOWED, 'Approve')
        self.bridge.sync()
        saved = self.store._read('SELECT text,source_id,sender_id,intent FROM applier_replies WHERE request_id=?', (drafted,))
        self.assertEqual(saved, [{'text': draft, 'source_id': approval['id'], 'sender_id': ALLOWED, 'intent': 'answer'}])
        revised = self.store.ask(1, 'owner-1', 'question', 'Anything else?', draft='No.')
        self.bridge.sync()
        post = next(m for m in self.asked() if 'Anything else?' in text(m))
        self.discord.say(thread, ALLOWED, 'Say I can start in June.')
        self.bridge.sync()
        self.assertEqual(self.store.latest_reply(revised), {'version': 1, 'text': 'Say I can start in June.', 'source_kind': 'discord', 'intent': 'change_request'})
        self.assertEqual((post['content'], post['components']), ('Job 1: Asked for changes: Say I can start in June. The Applier will send a new draft.', []))

    def test_request_changes_sends_feedback_and_the_new_draft_comes_as_a_new_question(self):
        request = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='I am thrilled to apply.')
        self.bridge.sync()
        post = self.asked()[0]
        modal = self.press(post, 'Request changes')
        self.assertEqual((modal['type'], modal['data']['components'][0]['components'][0]['label']), (9, 'What should change?'))
        self.assertNotIn('value', modal['data']['components'][0]['components'][0])
        update = self.press(post, 'Request changes', form='Less hype, name the payments project')
        self.assertEqual(update['data']['content'], 'Job 1: Asked for changes: Less hype, name the payments project. The Applier will send a new draft.')
        self.assertEqual(self.store.latest_reply(request)['intent'], 'change_request')
        rewrite = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='I rebuilt a payments service.')
        self.bridge.sync()
        posts = self.asked()
        self.assertEqual(len(posts), 2)
        self.assertIn('> I rebuilt a payments service.', text(posts[1]))
        self.assertEqual(self.press(posts[1], 'Approve draft')['data']['content'], 'Job 1: Answered: I rebuilt a payments service.')
        self.assertEqual(self.store.latest_reply(rewrite), {'version': 1, 'text': 'I rebuilt a payments service.', 'source_kind': 'discord', 'intent': 'answer'})

    def test_the_approve_button_saves_the_draft_and_write_my_own_prefills_it(self):
        draft = 'I build reliable backend systems.'
        request = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft=draft)
        self.bridge.sync()
        post = self.asked()[0]
        modal = self.press(post, 'Write my own')
        self.assertEqual(modal['data']['components'][0]['components'][0]['value'], draft)
        self.assertEqual(self.press(post, 'Approve draft')['data']['content'], f'Job 1: Answered: {draft}')
        self.assertEqual(self.store.latest_reply(request)['text'], draft)

    def test_write_my_own_saves_the_users_text_as_the_answer(self):
        request = self.store.ask(1, 'owner-1', 'question', 'Why Valon?', field='Why Valon', draft='I build reliable backend systems.')
        self.bridge.sync()
        update = self.press(self.asked()[0], 'Write my own', form='I want to work on mortgages.')
        self.assertEqual(update['data']['content'], 'Job 1: Answered: I want to work on mortgages.')
        self.assertEqual(self.store.latest_reply(request), {'version': 1, 'text': 'I want to work on mortgages.', 'source_kind': 'discord', 'intent': 'answer'})

    def test_ok_without_a_draft_is_saved_as_written(self):
        request = self.ask('Can you start in June?')
        self.bridge.sync()
        self.assertEqual(list(buttons(self.asked()[0])), ['Other', 'Ignore job'])
        self.discord.say(self.thread(), ALLOWED, 'ok')
        self.bridge.sync()
        self.assertEqual(self.store.latest_reply(request), {'version': 1, 'text': 'ok', 'source_kind': 'discord', 'intent': 'answer'})

    def test_a_long_question_is_shortened_but_its_draft_is_shown_whole(self):
        draft = 'd' * 1200
        request = self.store.ask(1, 'owner-1', 'question', 'q' * 5000, draft=draft)
        embed = question_message(self.store.request(request), 'https://example.test/job')['embeds'][0]
        self.assertLessEqual(len(embed['description']), 4096)
        self.assertIn('> ' + draft, embed['description'])
        with self.assertRaisesRegex(ValueError, 'at most 1200'):
            self.store.ask(1, 'owner-1', 'question', 'Other?', draft='d' * 1201)

    def test_a_long_review_fits_discord_limits_and_shows_long_answers_whole(self):
        fields = [(f'Question {i}', f'answer {i}') for i in range(60)] + [('Cover letter', 'c' * 3000)]
        messages = review_messages(1, 'https://example.test/job', fields, ref='1:abc')
        for message in messages:
            self.assertLessEqual(len(message['embeds']), 10)
            self.assertLessEqual(len(text(message)), 6000)
            self.assertTrue(all(len(e.get('fields', [])) <= 25 for e in message['embeds']))
        self.assertIn('c' * 3000, '\n'.join(text(m) for m in messages))
        self.assertEqual([bool(m.get('components')) for m in messages], [False] * (len(messages) - 1) + [True])

    def test_the_ignore_button_asks_once_more_then_skips_and_closes_the_questions(self):
        request = self.ask()
        self.bridge.sync()
        post = self.asked()[0]
        ask = self.press(post, 'Ignore job')
        self.assertEqual(ask['data']['flags'], 64)
        self.assertIsNone(self.store.skip(1))
        self.assertEqual(self.press({'components': ask['data']['components']}, 'Yes, ignore job 1')['type'], 7)
        skip = self.store.skip(1)
        self.assertEqual((skip['source_kind'], skip['sender_id']), ('discord', ALLOWED))
        self.assertEqual(self.store.request(request)['state'], 'withdrawn')
        with self.assertRaisesRegex(ValueError, 'skipped'):
            self.store.reply(request, 'June 1', source_kind='discord', source_id='m9', sender_id=ALLOWED)
        self.bridge.sync()
        self.assertEqual((post['content'], post['components']), ('Job 1: Closed: this job was skipped.', []))
        self.assertIn('already skipped', self.press({'components': [{'type': 1, 'components': [{'label': 'Ignore job', 'custom_id': 'ignore:1'}]}]}, 'Ignore job')['data']['content'])

    def test_a_job_ignored_from_its_question_gets_the_note_the_ignore_promised(self):
        self.ask()
        self.bridge.sync()
        ask = self.press(self.asked()[0], 'Ignore job')
        promise = self.press({'components': ask['data']['components']}, 'Yes, ignore job 1')['data']['content']
        self.assertIn('A note follows in its thread', promise)
        self.store.outbox_result(1)
        self.bridge.sync()
        self.bridge.sync()
        self.assertEqual(sum('Skipped as you asked' in m['content'] for m in self.bot_posts()), 1)

    def test_a_closed_posting_is_recorded_without_asking_and_announced_once(self):
        with self.assertRaisesRegex(ValueError, 'exact page sentence'):
            self.store.record_closed(1, 'owner-1', 'The page looks odd.')
        self.store.record_closed(1, 'owner-1', 'This job has expired on Indeed')
        self.assertEqual(self.store.outbox(pending=True)[0]['kind'], 'posting_closed')
        self.thread()
        self.store.outbox_result(1)
        self.bridge.sync()
        self.bridge.sync()
        closed = [m['content'] for m in self.bot_posts() if 'posting is closed' in m['content']]
        self.assertEqual(len(closed), 1)
        self.assertIn('This job has expired on Indeed', closed[0])

    def test_a_finished_skip_is_announced_once_in_the_job_thread(self):
        request = self.ask()
        self.bridge.sync()
        self.store.reply(request, 'skip this one', source_kind='discord', source_id='m1', sender_id=ALLOWED)
        self.store.record_skip(1, 'owner-1', request, 1)
        self.bridge.sync()
        self.assertFalse(any('Skipped' in m['content'] for m in self.discord.messages[self.thread()]))
        self.store.outbox_result(1)
        self.bridge.sync()
        self.bridge.sync()
        self.assertEqual(sum('Skipped as you asked' in m['content'] for m in self.discord.messages[self.thread()]), 1)

    def test_blocker_shows_attempts_and_one_press_replies(self):
        request = self.store.ask(1, 'owner-1', 'blocker', 'Portfolio field clears itself', field='portfolio', blocker_type='broken_component',
                                 evidence={'attempts': ['form_input', 'computer', 'javascript_tool'], 'error': 'value resets'})
        message = question_message(self.store.request(request), 'https://example.test/job')
        self.assertIn('Tried: form_input; computer; javascript_tool', text(message))
        self.assertEqual(list(buttons(message)), ['Done', 'Other', 'Ignore job'])
        unknown = self.store.ask(1, 'owner-1', 'blocker', 'Unclear result', blocker_type='submit_unknown')
        message = question_message(self.store.request(unknown), '')
        self.assertEqual(list(buttons(message)), ['Submitted', 'Not submitted', 'Other'])
        self.assertNotIn('url', message['embeds'][0])
        # Whether Submit went through is asked only once no confirmation email came in time.
        with self.store._write() as db:
            db.execute("UPDATE applier_requests SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (unknown,))
        self.bridge.sync()
        post = next(m for m in self.asked() if 'Unclear result' in text(m))
        self.press(post, 'Not submitted')
        self.assertEqual(self.store.latest_reply(unknown)['text'], 'not submitted')


class EvidenceShotTests(unittest.TestCase):
    """Once Jobfeed records a submitted job, its confirmation screenshot follows in the job's thread."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ApplierRuntime(self.root)
        self.discord = FakeDiscord()
        self.config = PipelineConfig(resume_root=self.root, jobfeed_root=self.root / 'jobfeed', jobfeed_db=self.root / 'jobfeed.sqlite',
                                     applications_root=self.root / 'applications')
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT, url TEXT)')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?)', [(i, 'Acme', 'Platform Engineer', f'https://example.com/jobs/{i}') for i in (1, 2)])
        ApplicationQueue(self.config.jobfeed_db).initialize()
        self.bridge = DiscordBridge(self.root, http=self.discord, channel_id='general', allowed_user_ids=[ALLOWED],
                                    job_names=lambda job_id: 'Acme · Platform Engineer', job_evidence=confirmation_reader(self.config))

    def recorded(self, job_id=1, kind='website_screenshot', *, thread=True):
        """Submit the job and record it in Jobfeed, with its confirmation saved in its own application folder."""
        folder = self.config.applications_root / f'job-{job_id}'
        write_confirmation(folder, job_id, f'https://example.com/jobs/{job_id}', kind=kind)
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute("INSERT INTO application_queue(job_id,state,application_folder_relpath) VALUES(?,'Submitted',?)", (job_id, f'job-{job_id}'))
        owner = f'owner-{job_id}'
        self.store.bind(job_id, owner, 'batch', 'opus')
        self.store.register_tab(job_id, owner, 5, 'https://example.test/job')
        self.store.readback(job_id, owner, 'Sponsorship', 'No')
        self.store.progress(job_id, owner, 'ready for review')
        if thread:
            self.bridge.sync()
        self.store.authorize_submit(job_id, source_kind='test', event_id=f'e{job_id}')
        self.store.request_submit(job_id, owner)
        self.store.record_submit_outcome(job_id, owner, 'submitted', 'Thanks for applying')
        self.store.outbox_result(job_id)
        return folder

    def outside_image(self):
        path = self.config.applications_root / 'outside.png'
        Image.new('RGB', (100, 50), 'black').save(path)
        return path

    def posts(self, job_id=1):
        return [m for m in self.discord.messages[self.bridge.thread_for(job_id)] if m['author']['bot']]

    def shots(self, job_id=1):
        return [m for m in self.posts(job_id) if m.get('attachments')]

    def skipped(self, events):
        return [e['reason'] for e in events if e['event'] == 'evidence_shot_skipped']

    def test_a_job_that_left_the_queue_is_found_through_its_applier_folder(self):
        folder = self.recorded()
        with closing(sqlite3.connect(self.config.jobfeed_db)) as db, db:
            db.execute('DELETE FROM application_queue WHERE job_id=1')
        with closing(sqlite3.connect(self.root / '.pipeline/applier-runtime.sqlite3')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS applier_launches(launch_id TEXT, job_id INTEGER, config TEXT)')
            db.execute('INSERT INTO applier_launches(launch_id,job_id,config) VALUES(?,?,?)', ('owner-1', 1, json.dumps({'cwd': str(folder / 'claude-applier')})))
        self.bridge.sync()
        self.assertEqual(len(self.shots()), 1)

    def test_a_recorded_job_gets_one_screenshot_with_a_caption_after_its_record(self):
        folder = self.recorded()
        self.bridge.sync()
        self.bridge.sync()
        shots = self.shots()
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]['content'], 'Confirmation page of Acme · Platform Engineer (job 1): <https://example.com/jobs/1/confirmation>')
        self.assertEqual(shots[0]['attachments'], [{'filename': 'confirmation.png', 'content_type': 'image/png', 'data': (folder / 'confirmation.png').read_bytes()}])
        contents = [m['content'] for m in self.posts()]
        self.assertEqual(contents.count(RECORDED_TEXT), 1)
        self.assertLess(contents.index(RECORDED_TEXT), contents.index(shots[0]['content']))

    def test_a_job_recorded_before_screenshots_gets_one_next_pass_and_a_job_without_a_thread_gets_none(self):
        self.recorded()
        self.recorded(2, thread=False)
        self.bridge.job_evidence = None
        self.bridge.sync()
        self.assertEqual(self.shots(), [])
        self.bridge.job_evidence = confirmation_reader(self.config)
        self.bridge.sync()
        self.bridge.sync()
        self.assertEqual(len(self.shots()), 1)
        self.assertEqual(len(self.discord.threads), 1, 'job 2 never had a thread, and none was made for its screenshot')

    def test_a_missing_screenshot_is_skipped_once_and_the_record_is_still_posted(self):
        (self.recorded() / 'confirmation.png').unlink()
        events = self.bridge.sync() + self.bridge.sync()
        self.assertEqual(self.skipped(events), ['artifact_missing_or_unreadable'])
        self.assertEqual(self.shots(), [])
        self.assertEqual([m['content'] for m in self.posts()].count(RECORDED_TEXT), 1)

    def test_a_screenshot_too_large_for_discord_is_skipped(self):
        folder = self.recorded()
        Image.frombytes('RGB', (1800, 1800), os.urandom(1800 * 1800 * 3)).save(folder / 'confirmation.png')
        self.assertGreater((folder / 'confirmation.png').stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(self.skipped(self.bridge.sync()), ['screenshot_too_large'])
        self.assertEqual(self.shots(), [])

    def test_a_screenshot_outside_its_folder_is_refused(self):
        folder = self.recorded()
        self.outside_image()
        receipt = json.loads((folder / 'submission-receipt.json').read_text())
        receipt['confirmation']['artifact'] = '../outside.png'
        (folder / 'submission-receipt.json').write_text(json.dumps(receipt))
        self.assertEqual(self.skipped(self.bridge.sync()), ['artifact_outside_application'])
        self.assertEqual(self.shots(), [])

    def test_a_screenshot_behind_a_symlink_is_refused(self):
        folder = self.recorded()
        (folder / 'confirmation.png').unlink()
        (folder / 'confirmation.png').symlink_to(self.outside_image())
        self.assertEqual(self.skipped(self.bridge.sync()), ['symlink_not_allowed'])
        self.assertEqual(self.shots(), [])

    def test_an_email_confirmation_posts_no_picture(self):
        self.recorded(kind='confirmation_email')
        events = self.bridge.sync() + self.bridge.sync()
        self.assertEqual(self.shots(), [])
        self.assertEqual(self.skipped(events), [])
        self.assertIn(RECORDED_TEXT, [m['content'] for m in self.posts()])


class HttpClientTests(unittest.TestCase):
    def send(self, *args):
        sent = []

        class Response(io.BytesIO):
            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            sent.append(request)
            return Response(b'{"id": "5"}')
        with patch('pipeline.applier_discord.urllib.request.urlopen', urlopen):
            self.assertEqual(http_client('secret')(*args), {'id': '5'})
        return sent[0]

    def test_a_file_goes_up_as_multipart_with_payload_json_and_files_0(self):
        image = b'\x89PNG synthetic bytes'
        request = self.send('POST', '/channels/7/messages', {'content': 'Caption'}, ('confirmation.png', 'image/png', image))
        self.assertEqual(request.full_url, 'https://discord.com/api/v10/channels/7/messages')
        self.assertEqual(request.get_header('Authorization'), 'Bot secret')
        content_type = request.get_header('Content-type')
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        form = BytesParser(policy=policy.default).parsebytes(f'Content-Type: {content_type}\r\n\r\n'.encode() + request.data)
        parts = {part.get_param('name', header='content-disposition'): part for part in form.iter_parts()}
        self.assertEqual(list(parts), ['payload_json', 'files[0]'])
        self.assertEqual(json.loads(parts['payload_json'].get_content()), {'content': 'Caption', 'attachments': [{'id': 0, 'filename': 'confirmation.png'}]})
        self.assertEqual(parts['files[0]'].get_filename(), 'confirmation.png')
        self.assertEqual(parts['files[0]'].get_content_type(), 'image/png')
        self.assertEqual(parts['files[0]'].get_payload(decode=True), image)

    def test_a_message_without_a_file_stays_json(self):
        request = self.send('POST', '/channels/7/messages', {'content': 'Hi'})
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data), {'content': 'Hi'})


if __name__ == '__main__':
    unittest.main()
