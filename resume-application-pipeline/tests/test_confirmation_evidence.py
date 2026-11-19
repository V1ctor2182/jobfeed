import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applying_pipeline_fixture import applying_pipeline
from confirmation_fixture import write_confirmation
from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation
from pipeline.jobfeed import Job
from pipeline.lifecycle import ApplicationState
from test_progress_monitor import Fixture


class ConfirmationArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'application'
        self.url = 'https://example.com/jobs/1'

    def test_screenshot_and_original_email_are_readable(self):
        for kind in ('website_screenshot', 'confirmation_email'):
            with self.subTest(kind=kind):
                write_confirmation(self.folder, kind=kind)
                evidence = read_confirmation(self.folder, 1, self.url)
                self.assertEqual(evidence.kind, kind)
                self.assertIn('application has been received', evidence.confirmation_text)
                self.assertGreater(len(evidence.artifact_bytes), 100)

    def test_prose_does_not_count_as_evidence(self):
        self.folder.mkdir()
        (self.folder / 'submission-receipt.json').write_text(json.dumps({'job_id': 1, 'job_url': self.url, 'success_evidence': 'Application submitted'}))
        with self.assertRaisesRegex(EvidenceValidationError, 'artifact_required'):
            read_confirmation(self.folder, 1, self.url)

    def test_rejects_job_mismatch_and_changed_observation(self):
        receipt = write_confirmation(self.folder)
        with self.assertRaisesRegex(EvidenceValidationError, 'job_mismatch'):
            read_confirmation(self.folder, 2, self.url)
        with self.assertRaisesRegex(EvidenceValidationError, 'job_mismatch'):
            read_confirmation(self.folder, 1, self.url + '2')
        receipt['confirmation']['text'] = 'Application submitted to a different job'
        (self.folder / 'submission-receipt.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(EvidenceValidationError, 'observation_mismatch'):
            read_confirmation(self.folder, 1, self.url)

    def test_rejects_missing_corrupt_and_symlinked_image(self):
        for content in (None, b'not an image', b'\x89PNG\r\n\x1a\n'):
            write_confirmation(self.folder)
            image = self.folder / 'confirmation.png'
            image.unlink()
            if content is not None:
                image.write_bytes(content)
            with self.assertRaises(EvidenceValidationError):
                read_confirmation(self.folder, 1, self.url)
        write_confirmation(self.folder)
        outside = self.folder.parent / 'outside.png'
        outside.write_bytes((self.folder / 'confirmation.png').read_bytes())
        (self.folder / 'confirmation.png').unlink()
        (self.folder / 'confirmation.png').symlink_to(outside)
        with self.assertRaisesRegex(EvidenceValidationError, 'symlink'):
            read_confirmation(self.folder, 1, self.url)

    def test_path_traversal_is_rejected(self):
        receipt = write_confirmation(self.folder)
        receipt['confirmation']['artifact'] = '../private.png'
        (self.folder / 'submission-receipt.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(EvidenceValidationError, 'outside_application'):
            read_confirmation(self.folder, 1, self.url)

    def test_account_success_and_negative_confirmation_are_rejected(self):
        for kind in ('website_screenshot', 'confirmation_email'):
            for text in ('Your account successfully created', 'Application not received', 'Application submitted? No, we could not submit your application.'):
                with self.subTest(kind=kind, text=text):
                    write_confirmation(self.folder, kind=kind, text=text)
                    with self.assertRaisesRegex(EvidenceValidationError, 'acknowledgement_missing'):
                        read_confirmation(self.folder, 1, self.url)

    def test_email_must_include_exact_posting_and_original_message_id(self):
        write_confirmation(self.folder, kind='confirmation_email')
        path = self.folder / 'confirmation.eml'
        raw = path.read_text()
        path.write_text(raw.replace(self.url, 'https://example.com/jobs/other'))
        with self.assertRaisesRegex(EvidenceValidationError, 'job_link_required'):
            read_confirmation(self.folder, 1, self.url)
        path.write_text(raw.replace('<synthetic-1@example.com>', '<other@example.com>'))
        with self.assertRaisesRegex(EvidenceValidationError, 'invalid_confirmation_email'):
            read_confirmation(self.folder, 1, self.url)

    def test_email_url_prefix_and_query_suffix_do_not_match(self):
        for wrong in (self.url + '0', self.url + '?another=job'):
            write_confirmation(self.folder, kind='confirmation_email')
            path = self.folder / 'confirmation.eml'
            path.write_text(path.read_text().replace(self.url, wrong))
            with self.assertRaisesRegex(EvidenceValidationError, 'job_link_required'):
                read_confirmation(self.folder, 1, self.url)

    def test_jpeg_capture_is_displayable_and_extension_must_match(self):
        from PIL import Image
        receipt = write_confirmation(self.folder)
        Image.new('RGB', (100, 50), 'white').save(self.folder / 'confirmation.jpg')
        receipt['confirmation']['artifact'] = 'confirmation.jpg'
        (self.folder / 'submission-receipt.json').write_text(json.dumps(receipt))
        self.assertEqual(read_confirmation(self.folder, 1, self.url).media_type, 'image/jpeg')
        (self.folder / 'confirmation.jpg').rename(self.folder / 'confirmation.png')
        receipt['confirmation']['artifact'] = 'confirmation.png'
        (self.folder / 'submission-receipt.json').write_text(json.dumps(receipt))
        with self.assertRaises(EvidenceValidationError):
            read_confirmation(self.folder, 1, self.url)


class ConfirmationPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobfeed = Mock(**{'get_job.return_value': Job(42, 'Acme', 'Software Engineer', 'Build systems', 'https://example.test'), 'is_applied.return_value': False})
        self.pipeline = applying_pipeline(tmp.name, 42, 'Acme-SWE-2026-09-03', jobfeed=self.jobfeed)
        self.queue = self.pipeline.queue
        self.folder = self.pipeline.config.applications_root / 'Acme-SWE-2026-09-03'

    def test_finish_submission_rejects_prose(self):
        (self.folder / 'submission-receipt.json').write_text(json.dumps({'job_id': 42, 'success_evidence': 'Application submitted'}))
        result = self.pipeline.finish_submission(42)
        self.assertEqual(result['status'], 'submission_unknown')
        self.assertEqual(self.queue.get(42).state, ApplicationState.SUBMISSION_UNKNOWN)
        self.jobfeed.record_applied.assert_not_called()

    def test_finish_submission_accepts_persisted_artifacts(self):
        write_confirmation(self.folder, 42, self.jobfeed.get_job(42).url)
        with patch.object(self.pipeline, 'reconcile_applied', return_value=False):
            result = self.pipeline.finish_submission(42)
        self.assertEqual([c.args[0] for c in self.jobfeed.record_applied.call_args_list], [42])
        self.assertNotEqual(result['status'], 'submission_unknown')

    def test_user_report_stops_without_claiming_confirmation(self):
        with patch.object(self.pipeline, '_user_submission', return_value={'user_text': 'I submitted'}):
            result = self.pipeline.finish_submission(42)
        self.assertEqual(result['status'], 'submission_unknown')
        self.jobfeed.record_applied.assert_not_called()


class ConfirmationMonitorTests(unittest.TestCase):
    def test_legacy_prose_and_user_report_do_not_count_submitted(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            with sqlite3.connect(fixture.db) as db:
                db.execute("UPDATE jobs SET url='https://example.com/jobs/' || id")
            fixture.receipt()
            (fixture.root / 'applications/one/submission-receipt.json').write_text(json.dumps({'job_id':1,'success_evidence':'Application submitted'}))
            snapshot = fixture.monitor.tick(force=True, now='2026-10-21T18:00:00+00:00')
            self.assertFalse(snapshot['jobs'][0]['submitted'])
            self.assertEqual(snapshot['counts']['submitted'], 0)
            folder = fixture.root / 'applications/one'
            (folder / 'user-submission-confirmation.json').write_text(json.dumps({'job_id': 1, 'status':'submitted_user_confirmed', 'do_not_reapply':True, 'user_text':'I submitted', 'recorded_at':'2026-10-21T18:00:00+00:00'}))
            snapshot = fixture.monitor.tick(force=True, now='2026-10-21T18:01:00+00:00')
            self.assertTrue(snapshot['jobs'][0]['user_submitted'])
            self.assertFalse(snapshot['jobs'][0]['submission_complete'])
            self.assertEqual(snapshot['counts']['submitted'], 0)
            write_confirmation(folder)
            snapshot = fixture.monitor.tick(force=True, now='2026-10-21T18:02:00+00:00')
            self.assertEqual(snapshot['counts']['submitted'], 1)
