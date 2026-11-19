
import json
import multiprocessing
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pipeline import word_conversion as word


def fake_export(command, **kwargs):
    staged_source, staged_pdf = Path(command[-2]), Path(command[-1])
    root = staged_source.parent.parent
    descriptor = os.open(root / 'executing', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        with (root / 'observed-attempts.jsonl').open('a') as stream:
            stream.write(json.dumps({'source': str(staged_source), 'pdf': str(staged_pdf)}) + '\n')
        time.sleep(0.08)
        staged_pdf.write_bytes(b'%PDF-1.4\n' + staged_source.read_bytes())
        return SimpleNamespace(returncode=0)
    finally:
        (root / 'executing').unlink()


def child_convert(source, root, results):
    try:
        output = word.convert_document(Path(source), stage_root=Path(root), run=fake_export)
        results.put({'ok': True, 'bytes': output.read_bytes()})
    except Exception as exc:
        results.put({'ok': False, 'error': str(exc)})


def child_crash(source, root):
    def interrupted_export(*args, **kwargs):
        os._exit(7)
    word.convert_document(Path(source), stage_root=Path(root), run=interrupted_export)


class WordConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / 'word-stage'
        self.source = self.root / 'resume.docx'
        self.source.write_bytes(b'job-a-content')

    def test_read_only_status_does_not_create_state(self):
        self.assertEqual(word.status(self.stage), {
            'state': 'idle', 'stage_root': str(self.stage), 'live_connection_tested': False,
        })
        self.assertFalse(self.stage.exists())

    def test_parallel_same_basename_exports_are_distinct_and_serialized(self):
        second = self.root / 'second-job' / 'resume.docx'
        second.parent.mkdir()
        second.write_bytes(b'job-b-content')
        context = multiprocessing.get_context('spawn')
        results = context.Queue()
        children = [context.Process(target=child_convert, args=(str(source), str(self.stage), results))
                    for source in (self.source, second)]
        for child in children:
            child.start()
        try:
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
            outcomes = [results.get(timeout=1) for _ in children]
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join()
            results.close()
        self.assertEqual(sorted(outcomes, key=lambda value: value['bytes']), [
            {'ok': True, 'bytes': b'%PDF-1.4\njob-a-content'},
            {'ok': True, 'bytes': b'%PDF-1.4\njob-b-content'},
        ])
        attempts = [json.loads(line) for line in (self.stage / 'observed-attempts.jsonl').read_text().splitlines()]
        self.assertEqual(len({row['source'] for row in attempts}), 2)
        self.assertEqual(len({row['pdf'] for row in attempts}), 2)
        self.assertEqual(word.status(self.stage)['state'], 'idle')

    def test_timeout_blocks_retries_until_explicit_reconciliation(self):
        output = self.source.with_suffix('.pdf')
        output.write_bytes(b'%PDF-old-output')
        timeout = Mock(side_effect=subprocess.TimeoutExpired('osascript', 200))
        with self.assertRaises(word.ConversionNeedsReconciliation):
            word.convert_document(self.source, stage_root=self.stage, run=timeout)
        pending = word.status(self.stage)
        self.assertEqual(pending['state'], 'needs_reconciliation')
        self.assertEqual(pending['attempt']['failure'], 'TimeoutExpired')
        attempt = pending['attempt']['attempt_id']
        self.assertEqual(output.read_bytes(), b'%PDF-old-output')
        untouched = Mock(side_effect=AssertionError('A second export must not start'))
        with self.assertRaises(word.ConversionNeedsReconciliation):
            word.convert_document(self.source, stage_root=self.stage, run=untouched)
        with self.assertRaises(ValueError):
            word.reconcile('0' * 32, stage_root=self.stage, evidence='Confirmed idle', confirm_no_in_flight=True)
        with self.assertRaises(ValueError):
            word.reconcile(attempt, stage_root=self.stage, evidence='Confirmed idle', confirm_no_in_flight=False)
        result = word.reconcile(attempt, stage_root=self.stage,
                                evidence='Operator checked the exact document and no pending export remains',
                                confirm_no_in_flight=True)
        self.assertEqual(result, {'state': 'reconciled', 'attempt_id': attempt, 'files_preserved': True})
        self.assertTrue(Path(pending['attempt']['staged_source']).is_file())
        archive = json.loads((self.stage / 'reconciled' / f'{attempt}.json').read_text())
        self.assertEqual(archive['state'], 'reconciled')
        self.assertEqual(word.convert_document(self.source, stage_root=self.stage, run=fake_export).read_bytes(),
                         b'%PDF-1.4\njob-a-content')

    def test_nonzero_appleevent_failure_keeps_reservation_and_files(self):
        with self.assertRaises(word.ConversionNeedsReconciliation):
            word.convert_document(self.source, stage_root=self.stage,
                                  run=lambda *args, **kwargs: SimpleNamespace(returncode=1))
        pending = word.status(self.stage)
        self.assertEqual(pending['attempt']['state'], 'needs_reconciliation')
        self.assertTrue(Path(pending['attempt']['staged_source']).exists())

    def test_dead_process_does_not_authorize_resource_takeover(self):
        context = multiprocessing.get_context('spawn')
        child = context.Process(target=child_crash, args=(str(self.source), str(self.stage)))
        child.start()
        child.join(timeout=10)
        try:
            self.assertEqual(child.exitcode, 7)
            self.assertEqual(word.status(self.stage)['attempt']['state'], 'in_flight')
            with self.assertRaises(word.ConversionNeedsReconciliation):
                word.convert_document(self.source, stage_root=self.stage,
                                      run=Mock(side_effect=AssertionError('No takeover allowed')))
        finally:
            if child.is_alive():
                child.terminate()
                child.join()

    def test_missing_executable_is_known_unsent_and_releases_reservation(self):
        with self.assertRaisesRegex(word.WordConversionError, 'no Word export was started'):
            word.convert_document(self.source, stage_root=self.stage, run=Mock(side_effect=FileNotFoundError()))
        self.assertEqual(word.status(self.stage)['state'], 'idle')
        self.assertEqual(word.convert_document(self.source, stage_root=self.stage, run=fake_export).read_bytes(),
                         b'%PDF-1.4\njob-a-content')

    def test_invalid_pdf_does_not_replace_existing_output(self):
        output = self.source.with_suffix('.pdf')
        output.write_bytes(b'%PDF-existing')

        def invalid_export(command, **kwargs):
            Path(command[-1]).write_bytes(b'not a PDF')
            return SimpleNamespace(returncode=0)

        with self.assertRaisesRegex(word.WordConversionError, 'did not produce a PDF'):
            word.convert_document(self.source, stage_root=self.stage, run=invalid_export)
        self.assertEqual(output.read_bytes(), b'%PDF-existing')
        self.assertEqual(word.status(self.stage)['state'], 'idle')

    def test_paths_are_arguments_and_only_the_returned_document_is_saved(self):
        source = self.root / 'a "quoted" $file.docx'
        source.write_bytes(b'quoted-job')
        seen = []

        def inspect_export(command, **kwargs):
            seen.append((command, kwargs['input']))
            Path(command[-1]).write_bytes(b'%PDF-quoted-job')
            return SimpleNamespace(returncode=0)

        self.assertEqual(word.convert_document(source, stage_root=self.stage, run=inspect_export).read_bytes(),
                         b'%PDF-quoted-job')
        command, script = seen[0]
        self.assertEqual(command[:2], ['osascript', '-'])
        self.assertNotIn(str(source), script)
        self.assertIn('if full name of ownedDocument is not expectedPath then', script)
        self.assertIn('save as ownedDocument file name outputPath file format format PDF', script)
        self.assertIn('close ownedDocument saving no', script)
        self.assertNotIn('active document', script)

    def test_cli_status_and_reconcile_stay_offline(self):
        stdout = StringIO()
        with patch.object(word, 'default_stage_root', return_value=self.stage), redirect_stdout(stdout), \
                patch.object(word.subprocess, 'run', side_effect=AssertionError('No app process allowed')):
            self.assertEqual(word.main(['status']), 0)
        self.assertEqual(json.loads(stdout.getvalue())['state'], 'idle')
        self.assertFalse(self.stage.exists())
        stderr = StringIO()
        with patch.object(word, 'default_stage_root', return_value=self.stage), redirect_stderr(stderr):
            self.assertEqual(word.main(['reconcile', '0' * 32, '--evidence', 'idle', '--confirm-no-in-flight']), 1)
        self.assertIn('does not match', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
