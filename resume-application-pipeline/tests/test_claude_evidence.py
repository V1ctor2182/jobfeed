import base64
import json
from pathlib import Path
import tempfile
import unittest

from pipeline.applier_mcp import ApplierTools
from pipeline.applier_runtime import ApplierRuntime
from pipeline.claude_applier import _SCHEMA as LAUNCH_SCHEMA
from pipeline.claude_evidence import latest_capture, transcript_path, write_receipt

SHOT = (Path(__file__).parent / 'fixtures' / 'claude' / 'screenshot.jpg').read_bytes()
PAGE_TEXT = 'Synthetic application\nThank you. Your application has been received.\nReference 123'


def entry(timestamp, kind, part):
    return json.dumps({'type': kind, 'timestamp': timestamp, 'message': {'role': kind, 'content': [part]}})


def transcript(path, *, after=True, tabs=(7, 7)):
    when = '2026-10-25T20:00:05.000Z' if after else '2026-10-25T19:59:00.000Z'
    lines = [
        entry('2026-10-25T20:00:01.000Z', 'assistant', {'type': 'tool_use', 'id': 'shot-1', 'name': 'mcp__claude-in-chrome__computer', 'input': {'action': 'screenshot', 'tabId': tabs[0]}}),
        entry(when, 'user', {'type': 'tool_result', 'tool_use_id': 'shot-1', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(SHOT).decode()}}]}),
        entry('2026-10-25T20:00:06.000Z', 'assistant', {'type': 'tool_use', 'id': 'page-1', 'name': 'mcp__claude-in-chrome__get_page_text', 'input': {'tabId': tabs[1]}}),
        entry(when, 'user', {'type': 'tool_result', 'tool_use_id': 'page-1', 'content': [{'type': 'text', 'text': PAGE_TEXT}]}),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n')


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_the_receipt_passes_jobfeed_validation_with_unchanged_screenshot_bytes(self):
        path = self.root / 't.jsonl'
        transcript(path)
        shot, page = latest_capture(path, '2026-10-25T20:00:00+00:00')
        folder = self.root / 'job'
        write_receipt(folder, job_id=9, job_url='https://board.example.test/jobs/9', source_url='https://acme.example.test/thanks',
                      acknowledgement='Your application has been received.', shot=shot, page=page)
        self.assertEqual((folder / 'confirmation.jpg').read_bytes(), SHOT)
        try:
            from jobfeed.submission_evidence import read_confirmation
        except ImportError:
            self.skipTest('jobfeed is not importable here')
        evidence = read_confirmation(folder, 9, 'https://board.example.test/jobs/9')
        self.assertIn('received', str(evidence))

    def test_results_from_before_submit_are_ignored(self):
        path = self.root / 't.jsonl'
        transcript(path, after=False)
        self.assertEqual(latest_capture(path, '2026-10-25T20:00:00+00:00'), (None, None))

    def test_error_results_other_tabs_and_non_screenshot_images_are_ignored(self):
        path = self.root / 't.jsonl'
        transcript(path)
        lines = path.read_text().splitlines()
        click = json.loads(lines[0]); click['message']['content'][0]['input']['action'] = 'left_click'
        failed = json.loads(lines[3]); failed['message']['content'][0]['is_error'] = True
        path.write_text('\n'.join([json.dumps(click), lines[1], lines[2], json.dumps(failed)]) + '\n')
        self.assertEqual(latest_capture(path, '2026-10-25T20:00:00+00:00'), (None, None))
        transcript(path)
        self.assertEqual(latest_capture(path, '2026-10-25T20:00:00+00:00', tab_id=8), (None, None))
        self.assertIsNotNone(latest_capture(path, '2026-10-25T20:00:00+00:00', tab_id=7)[0])

    def test_an_acknowledgement_not_on_the_page_is_refused(self):
        path = self.root / 't.jsonl'
        transcript(path)
        shot, page = latest_capture(path, '2026-10-25T20:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'appear exactly'):
            write_receipt(self.root / 'job', job_id=9, job_url='https://board.example.test/jobs/9', source_url='https://x.test/',
                          acknowledgement='Offer accepted', shot=shot, page=page)

    def test_a_screenshot_and_page_read_from_different_tabs_are_refused(self):
        path = self.root / 't.jsonl'
        transcript(path, tabs=(7, 8))
        shot, page = latest_capture(path, '2026-10-25T20:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'registered form tab'):
            write_receipt(self.root / 'job', job_id=9, job_url='https://board.example.test/jobs/9', source_url='https://x.test/',
                          acknowledgement='Your application has been received.', shot=shot, page=page)

    def test_the_tool_finds_the_transcript_through_the_launch_and_needs_a_submit(self):
        store = ApplierRuntime(self.root / 'state')
        store.bind(9, 'a' * 32, 'batch', 'opus')
        cwd = self.root / 'work'
        with store._write() as db:
            db.executescript(LAUNCH_SCHEMA)
            db.execute("INSERT INTO applier_launches(launch_id,job_id,name,config,background_id,session_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       ('a' * 32, 9, 'applier-9', json.dumps({'cwd': str(cwd)}), 'aaaaaaaa', 'sess-1', 'running', 'x', 'x'))
        home = self.root / 'home'
        store.register_tab(9, 'a' * 32, 7, 'https://acme.example.test/apply')
        tools = ApplierTools(store, 9, 'a' * 32, evidence_dir=self.root / 'job', job_url='https://board.example.test/jobs/9', transcripts_home=home)
        with self.assertRaisesRegex(ValueError, 'only after an authorized Submit'):
            tools.record_confirmation('https://acme.example.test/thanks', 'Your application has been received.')
        store.authorize_submit(9, source_kind='test', event_id='e9')
        tools.request_submit()
        with store._write() as db:
            db.execute("UPDATE applier_submits SET requested_at='2026-10-25T20:00:00+00:00' WHERE job_id=9")
        transcript(transcript_path(cwd, 'sess-1', home))
        result = tools.record_confirmation('https://acme.example.test/thanks', 'Your application has been received.')
        self.assertEqual(result['saved']['artifact'], 'confirmation.jpg')
        self.assertTrue((self.root / 'job' / 'submission-receipt.json').exists())

    def test_a_job_board_tab_beside_the_form_tab_does_not_block_the_receipt(self):
        store = ApplierRuntime(self.root / 'state')
        store.bind(9, 'a' * 32, 'batch', 'opus')
        cwd, home = self.root / 'work', self.root / 'home'
        with store._write() as db:
            db.executescript(LAUNCH_SCHEMA)
            db.execute("INSERT INTO applier_launches(launch_id,job_id,name,config,background_id,session_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       ('a' * 32, 9, 'applier-9', json.dumps({'cwd': str(cwd)}), 'aaaaaaaa', 'sess-1', 'running', 'x', 'x'))
        store.register_tab(9, 'a' * 32, 6, 'https://board.example.test/jobs/9')
        store.register_tab(9, 'a' * 32, 7, 'https://acme.example.test/apply')
        tools = ApplierTools(store, 9, 'a' * 32, evidence_dir=self.root / 'job', job_url='https://board.example.test/jobs/9', transcripts_home=home)
        store.authorize_submit(9, source_kind='test', event_id='e9')
        tools.request_submit()
        with store._write() as db:
            db.execute("UPDATE applier_submits SET requested_at='2026-10-25T20:00:00+00:00' WHERE job_id=9")
        transcript(transcript_path(cwd, 'sess-1', home))
        with self.assertRaisesRegex(ValueError, 'exactly one registered form tab'):
            tools.record_confirmation('https://elsewhere.example.test/thanks', 'Your application has been received.')
        result = tools.record_confirmation('https://acme.example.test/thanks', 'Your application has been received.')
        self.assertEqual(result['saved']['artifact'], 'confirmation.jpg')

    def test_a_page_read_taken_before_the_confirmation_rendered_can_be_retaken_even_after_an_unknown_outcome(self):
        store = ApplierRuntime(self.root / 'state')
        store.bind(9, 'a' * 32, 'batch', 'opus')
        cwd, home = self.root / 'work', self.root / 'home'
        with store._write() as db:
            db.executescript(LAUNCH_SCHEMA)
            db.execute("INSERT INTO applier_launches(launch_id,job_id,name,config,background_id,session_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       ('a' * 32, 9, 'applier-9', json.dumps({'cwd': str(cwd)}), 'aaaaaaaa', 'sess-1', 'running', 'x', 'x'))
        store.register_tab(9, 'a' * 32, 7, 'https://acme.example.test/apply')
        tools = ApplierTools(store, 9, 'a' * 32, evidence_dir=self.root / 'job', job_url='https://board.example.test/jobs/9', transcripts_home=home)
        store.authorize_submit(9, source_kind='test', event_id='e9')
        tools.request_submit()
        with store._write() as db:
            db.execute("UPDATE applier_submits SET requested_at='2026-10-25T20:00:00+00:00' WHERE job_id=9")
        path = transcript_path(cwd, 'sess-1', home)
        path.parent.mkdir(parents=True)
        page = lambda at, call, text: [  # noqa: E731
            entry(at, 'assistant', {'type': 'tool_use', 'id': call, 'name': 'mcp__claude-in-chrome__get_page_text', 'input': {'tabId': 7}}),
            entry(at, 'user', {'type': 'tool_result', 'tool_use_id': call, 'content': [{'type': 'text', 'text': text}]})]
        shot = [entry('2026-10-25T20:00:03.000Z', 'assistant', {'type': 'tool_use', 'id': 'shot-1', 'name': 'mcp__claude-in-chrome__computer', 'input': {'action': 'screenshot', 'tabId': 7}}),
                entry('2026-10-25T20:00:04.000Z', 'user', {'type': 'tool_result', 'tool_use_id': 'shot-1', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(SHOT).decode()}}]})]
        acknowledgement = 'Your application has been received.'
        path.write_text('\n'.join(page('2026-10-25T20:00:01.000Z', 'page-1', 'Application\nFull Name\nSubmit Application') + shot) + '\n')
        with self.assertRaisesRegex(ValueError, 'get_page_text again') as refused:
            tools.record_confirmation('https://acme.example.test/apply', acknowledgement)
        self.assertIn('Keep the tab open', str(refused.exception))
        tools.record_submit_outcome('unknown', 'banner seen in the screenshot, not yet in the page text')
        path.write_text(path.read_text() + '\n'.join(page('2026-10-25T20:00:09.000Z', 'page-2', PAGE_TEXT)) + '\n')
        tools.record_confirmation('https://acme.example.test/apply', acknowledgement)
        self.assertEqual(tools.record_submit_outcome('submitted', 'confirmation saved')['recorded'], 'submitted')
        self.assertEqual(store.submit_state(9)['state'], 'submitted')
        self.assertFalse(tools.request_submit()['authorized'])


if __name__ == '__main__':
    unittest.main()
