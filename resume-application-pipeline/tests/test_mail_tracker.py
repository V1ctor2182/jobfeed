import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from datetime import timedelta

from pipeline.mail_tracker import MailStore, body, classify, company_of, label_mail, sync, view

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)


def message(mid, subject, snippet, *, sender='no-reply@ashbyhq.com', name=None, when='2026-11-11T14:00:00+00:00'):
    millis = int(datetime.fromisoformat(when).timestamp() * 1000)
    names = [{'emailAddress': sender, 'displayName': name}] if name else []
    return {'id': mid, 'threadId': mid, 'sender': sender, 'addressDisplayNames': names, 'subject': subject, 'snippet': snippet,
            'internalDate': str(millis), 'viewUrl': f'https://mail.google.com/#all/{mid}'}


def pages(*groups):
    """A fake Gmail search that returns one page of messages per call."""
    calls = []

    def call(tool, request):
        calls.append(request)
        index = len(calls) - 1
        group = groups[index] if index < len(groups) else []
        page = {'threads': [{'id': m['id'], 'messages': [m]} for m in group]}
        if index + 1 < len(groups):
            page['nextPageToken'] = f'page-{index + 1}'
        return page
    call.calls = calls
    return call


class ClassifyTests(unittest.TestCase):
    def test_a_rejection_that_thanks_the_applicant_is_a_rejection(self):
        self.assertEqual(classify('Realm Alliance Application Update',
                                  'Thank you for taking the time to apply to Realm. After careful consideration, we&#39;ve decided to move forward with other candidates'), 'rejected')
        self.assertEqual(classify('Update on your Kyndryl Job Application', 'we regret to inform you that your application was not selected'), 'rejected')

    def test_received_interview_offer_and_action_mail(self):
        self.assertEqual(classify('USD Application Received', 'Thank you for your application for the Applications Analyst II'), 'applied')
        self.assertEqual(classify('Prescience Team 和 Alex 之间的 Round 2', '您的活动已预约'), 'interviewing')
        self.assertEqual(classify('Your offer letter from Orchard', 'We are pleased to offer you the role'), 'offer')
        self.assertEqual(classify('Roblox Application: Reminder to Verify Your Email Address', 'Please verify your email address'), 'action')

    def test_marketing_that_only_says_offer_is_not_an_application_stage(self):
        self.assertEqual(classify('Limited-Time Offer: 30% Off Your Photos', "Don't miss out on preserving your memories!"), 'other')


class CompanyTests(unittest.TestCase):
    def test_company_comes_from_the_display_name_then_the_domain_then_the_subject(self):
        self.assertEqual(company_of('no-reply@ashbyhq.com', 'Realm Hiring Team', 'Realm Alliance Application Update'), 'Realm')
        self.assertEqual(company_of('no-reply@pinterest.com', None, 'Your application'), 'pinterest')
        self.assertEqual(company_of('kyndryl@myworkday.com', None, 'Update on your Job Application'), 'kyndryl')
        self.assertEqual(company_of('no-reply@us.greenhouse-mail.io', None, 'Thank you for your interest in Ocrolus'), 'Ocrolus')
        self.assertIsNone(company_of('no-reply@us.greenhouse-mail.io', None, 'application update'))

    def test_company_survives_possessives_mail_systems_and_subject_first_names(self):
        self.assertEqual(company_of('no-reply@ashbyhq.com', "Notion's Recruiting Team", 'Thanks for applying'), 'Notion')
        self.assertEqual(company_of('no-reply-867@mail.pageuppeople.com', 'no-reply@sandiego.edu', 'USD Application Received'), 'sandiego')
        self.assertEqual(company_of('no-reply@us.greenhouse-mail.io', None, 'DoorDash Application Status'), 'DoorDash')
        self.assertEqual(company_of('no-reply@us.greenhouse-mail.io', None, 'Layer Health - Thank you for applying'), 'Layer Health')
        self.assertEqual(company_of('indeedapply@indeed.com', 'Indeed Apply', 'Indeed Application: Data Engineer'), None)
        self.assertEqual(company_of('no-reply@pinterest.com', None, 'Your application for University Grad Software Engineer 2027 (USA) at Pinterest'), 'pinterest')

    def test_mail_about_an_application_that_does_not_say_what_changed_is_an_update(self):
        from pipeline.mail_tracker import classify as sort
        self.assertEqual(sort('Your application for University Grad Software Engineer at Pinterest', 'Thank you so much for your interest in Pinterest. Our team reviewed your background and,'), 'update')
        self.assertEqual(sort('DoorDash Application Status', 'Hi Alex, an update is available'), 'update')


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MailStore(self.root)
        self.jobfeed_db = self.root / 'jobfeed.sqlite3'
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, title TEXT, url TEXT)')
            db.execute('CREATE TABLE real_job_applications(source_job_id INTEGER PRIMARY KEY, applied_at TEXT)')

    def applied(self, job_id, company, title, when):
        with closing(sqlite3.connect(self.jobfeed_db)) as db, db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (job_id, company, title, f'https://example.test/{job_id}'))
            db.execute('INSERT INTO real_job_applications VALUES(?,?)', (job_id, when))

    def stages(self):
        return {(c['company'], c['title']): c['stage'] for c in view(self.store, self.jobfeed_db, NOW)['applications']}

    def test_sync_saves_new_mail_once_and_stops_at_a_page_with_nothing_new(self):
        first = message('m1', 'USD Application Received', 'Thank you for your application')
        self.assertEqual(sync(self.store, pages([first])), 1)
        call = pages([first], [message('m0', 'older', 'Thank you for applying')])
        self.assertEqual(sync(self.store, call), 0)
        self.assertEqual(len(call.calls), 1)
        self.assertEqual([m['id'] for m in self.store.messages()], ['m1'])
        self.assertEqual(self.store.last_run()['new_messages'], 0)

    def test_sync_follows_pages_and_asks_only_for_recent_mail_after_the_first_run(self):
        call = pages([message('m2', 'Application received', 'x')], [message('m1', 'Application received', 'x')])
        self.assertEqual(sync(self.store, call), 2)
        self.assertEqual(call.calls[1]['pageToken'], 'page-1')
        self.assertIn('newer_than:45d', call.calls[0]['query'])
        later = pages([])
        sync(self.store, later)
        self.assertIn('newer_than:3d', later.calls[0]['query'])

    def test_a_failed_search_is_recorded_and_raised(self):
        def call(tool, request):
            raise RuntimeError('gmail is down')
        with self.assertRaises(RuntimeError):
            sync(self.store, call)
        self.assertIn('gmail is down', self.store.last_run()['error'])

    def test_mail_moves_an_applied_job_to_the_stage_it_reports(self):
        self.applied(1, 'Realm', 'Software Engineer', '2026-11-05T00:00:00+00:00')
        self.applied(2, 'Pinterest', 'University Grad Software Engineer', '2026-11-05T00:00:00+00:00')
        self.applied(3, 'Quiet Co', 'Engineer', '2026-10-01T00:00:00+00:00')
        self.applied(4, 'Fresh Co', 'Engineer', '2026-11-10T00:00:00+00:00')
        sync(self.store, pages([
            message('m1', 'Realm Alliance Application Update', 'we decided to move forward with other candidates', name='Realm Hiring Team'),
            message('m2', 'Thank you for applying to Realm', 'We received your application', name='Realm Hiring Team', when='2026-11-05T01:00:00+00:00'),
            message('m3', 'Interview with Pinterest', 'Please share your availability for a phone screen', sender='recruiting@pinterest.com'),
        ]))
        self.assertEqual(self.stages(), {('Realm', 'Software Engineer'): 'rejected', ('Pinterest', 'University Grad Software Engineer'): 'interviewing',
                                         ('Quiet Co', 'Engineer'): 'ghosted', ('Fresh Co', 'Engineer'): 'applied'})
        realm = next(c for c in view(self.store, self.jobfeed_db, NOW)['applications'] if c['company'] == 'Realm')
        self.assertEqual((realm['confirmed_by_email'], realm['email_count'], realm['source']), (True, 2, 'jobfeed'))

    def test_a_card_names_the_batch_its_applier_ran_in(self):
        self.applied(1, 'Realm', 'Software Engineer', '2026-11-05T00:00:00+00:00')
        self.applied(2, 'Orchard', 'Engineer', '2026-11-05T00:00:00+00:00')
        (self.root / '.pipeline').mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.root / '.pipeline' / 'applier-runtime.sqlite3')) as db, db:
            db.execute('CREATE TABLE applier_jobs(job_id INTEGER PRIMARY KEY, scope TEXT)')
            db.execute("INSERT INTO applier_jobs VALUES(1,'/somewhere/.pipeline/batches/autopilot.json')")
        cards = {c['job_id']: c['batch_id'] for c in view(self.store, self.jobfeed_db, NOW)['applications']}
        self.assertEqual(cards, {1: 'autopilot', 2: None})

    def test_mail_for_a_company_jobfeed_does_not_know_becomes_its_own_card(self):
        sync(self.store, pages([message('m1', 'USD Application Received', 'Thank you for your application', sender='no-reply@sandiego.edu')]))
        cards = view(self.store, self.jobfeed_db, NOW)['applications']
        self.assertEqual([(c['company'], c['job_id'], c['stage'], c['source'], c['confirmed_by_email']) for c in cards], [('sandiego', None, 'applied', 'email', True)])

    def test_mail_that_names_a_title_goes_only_to_that_job(self):
        self.applied(1, 'Orchard', 'Platform Engineer', '2026-11-05T00:00:00+00:00')
        self.applied(2, 'Orchard', 'Data Engineer', '2026-11-05T00:00:00+00:00')
        sync(self.store, pages([message('m1', 'Your application for Data Engineer', 'unfortunately we will not be moving forward', sender='jobs@orchard.com')]))
        self.assertEqual(self.stages(), {('Orchard', 'Platform Engineer'): 'applied', ('Orchard', 'Data Engineer'): 'rejected'})

    def test_the_models_label_replaces_the_keyword_rules(self):
        sync(self.store, pages([message('m1', 'Ace your next interview', 'Book a mock interview with a coach', name='Evan Hello Interview')]))
        self.assertEqual([m['status'] for m in self.store.messages()], ['interviewing'])
        self.assertEqual(label_mail(self.store, lambda emails: [{'n': 0, 'status': 'other', 'company': None}]), 1)
        self.assertEqual([(m['status'], m['company'], m['labelled_by']) for m in self.store.messages()], [('other', None, 'model')])
        self.assertEqual(view(self.store, self.jobfeed_db, NOW)['applications'], [])
        self.assertEqual(label_mail(self.store, lambda emails: self.fail('labelled mail is not asked about again')), 0)

    def test_a_model_answer_outside_the_fixed_labels_is_ignored(self):
        sync(self.store, pages([message('m1', 'USD Application Received', 'Thank you for your application'),
                                message('m2', 'Application received', 'Thanks', when='2026-11-10T14:00:00+00:00')]))
        answers = [{'n': 0, 'status': 'ignore previous instructions', 'company': 'x'}, {'n': 9, 'status': 'offer', 'company': 'x'},
                   {'n': 1, 'status': 'applied', 'company': 'y' * 200}]
        self.assertEqual(label_mail(self.store, lambda emails: answers), 0)
        self.assertEqual({m['labelled_by'] for m in self.store.messages()}, {'rules'})

    def test_a_failed_model_call_leaves_the_rules_in_charge(self):
        sync(self.store, pages([message('m1', 'USD Application Received', 'Thank you for your application')]))

        def ask(emails):
            raise RuntimeError('model is down')
        self.assertEqual(label_mail(self.store, ask), 0)
        self.assertEqual([(m['status'], m['labelled_by']) for m in self.store.messages()], [('applied', 'rules')])

    def test_the_model_sees_only_sender_subject_and_snippet(self):
        sync(self.store, pages([message('m1', 'Hello', 'World', name='Realm Hiring Team')]))
        seen = []
        label_mail(self.store, lambda emails: seen.extend(emails) or [])
        self.assertEqual(seen, [{'n': 0, 'from': 'no-reply@ashbyhq.com', 'name': 'Realm Hiring Team', 'subject': 'Hello', 'snippet': 'World'}])

    def test_after_an_outage_the_next_sync_looks_back_to_the_last_success(self):
        sync(self.store, pages([message('m1', 'Application received', 'x')]))
        call = pages([])
        sync(self.store, call, now=datetime.now(timezone.utc) + timedelta(days=9))
        self.assertIn('newer_than:11d', call.calls[0]['query'])

    def test_a_body_is_fetched_once_and_then_served_from_the_store(self):
        sync(self.store, pages([message('m1', 'Update on your application', 'Dear Alex', sender='kyndryl@myworkday.com')]))
        calls = []

        def call(tool, request):
            calls.append((tool, request))
            return {'messages': [{'id': 'm0', 'plaintextBody': 'older'}, {'id': 'm1', 'plaintextBody': 'Dear Alex,\r\n\r\nWe regret to inform you.', 'toRecipients': ['me@example.com']}]}
        first = body(self.store, 'm1', call)
        self.assertEqual((first['body'], first['recipients'], first['subject'], first['sender']),
                         ('Dear Alex,\n\nWe regret to inform you.', ['me@example.com'], 'Update on your application', 'kyndryl@myworkday.com'))
        self.assertEqual(calls, [('mcp__claude_ai_Gmail__get_thread', {'threadId': 'm1', 'messageFormat': 'PLAIN_TEXT'})])
        self.assertEqual(body(self.store, 'm1', lambda *a: self.fail('fetched twice'))['body'], first['body'])

    def test_only_a_message_the_sync_saved_can_be_opened(self):
        with self.assertRaisesRegex(ValueError, 'unknown message'):
            body(self.store, 'not-synced', lambda *a: self.fail('Gmail was asked for a message the sync never saved'))

    def test_other_mail_is_kept_out_of_the_inbox(self):
        sync(self.store, pages([message('m1', 'Limited-Time Offer: 30% Off', 'photos', sender='email@marketing.example.com')]))
        self.assertEqual(view(self.store, self.jobfeed_db, NOW)['messages'], [])


if __name__ == '__main__':
    unittest.main()
