"""Serialize Word exports and retain uncertain attempts for explicit reconciliation."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator


WORD_EXPORT = '''on run argv
    set inputPath to item 1 of argv
    set outputPath to item 2 of argv
    set expectedPath to (POSIX file inputPath) as text
    with timeout of 180 seconds
        tell application "Microsoft Word"
            set openedDocuments to open file name inputPath with read only
            if class of openedDocuments is list then
                set ownedDocument to item 1 of openedDocuments
            else
                set ownedDocument to openedDocuments
            end if
            if full name of ownedDocument is not expectedPath then
                error "word_document_identity_mismatch"
            end if
            save as ownedDocument file name outputPath file format format PDF
            close ownedDocument saving no
        end tell
    end timeout
end run
'''


class WordConversionError(RuntimeError):
    pass


class ConversionBusy(WordConversionError):
    pass


class ConversionNeedsReconciliation(WordConversionError):
    pass


def default_stage_root() -> Path:
    return Path.home() / 'Library/Containers/com.microsoft.Word/Data/tmp/docx2pdf'


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _exclusive(stage_root: Path, timeout: float) -> Iterator[None]:
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(stage_root / 'conversion.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConversionBusy('Word conversion is busy; no export was started') from None
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def status(stage_root: Path | None = None) -> dict:
    """Read local adapter state without probing or launching Word."""
    root = stage_root or default_stage_root()
    pending = root / 'pending.json'
    if not pending.exists():
        return {'state': 'idle', 'stage_root': str(root), 'live_connection_tested': False}
    try:
        attempt = json.loads(pending.read_text())
        if (not isinstance(attempt, dict)
                or not isinstance(attempt.get('attempt_id'), str)
                or re.fullmatch(r'[0-9a-f]{32}', attempt['attempt_id']) is None):
            raise ValueError('invalid attempt record')
    except (OSError, ValueError) as exc:
        raise ConversionNeedsReconciliation('Word adapter state is unreadable; preserve it for reconciliation') from exc
    return {'state': 'needs_reconciliation', 'stage_root': str(root), 'attempt': attempt,
            'live_connection_tested': False}


def _publish_pdf(staged: Path, output: Path) -> None:
    with staged.open('rb') as stream:
        if stream.read(5) != b'%PDF-':
            raise WordConversionError('Word did not produce a PDF; the existing output was preserved')
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{output.name}.', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target, staged.open('rb') as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def convert_document(source: Path, output: Path | None = None, *, stage_root: Path | None = None,
                     lock_timeout: float = 30, run: Callable | None = None) -> Path:
    source = source.resolve(strict=True)
    output = (output or source.with_suffix('.pdf')).resolve()
    if not source.is_file() or source.suffix.casefold() != '.docx':
        raise WordConversionError('Input must be an existing .docx file')
    if output == source or not output.parent.is_dir():
        raise WordConversionError('Output must have an existing directory and differ from the source')
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise ValueError('lock_timeout must be finite and nonnegative')
    run = run or subprocess.run
    root = stage_root or default_stage_root()
    if stage_root is None and not (Path.home() / 'Library/Containers/com.microsoft.Word').is_dir():
        raise WordConversionError('Microsoft Word container is missing; no application was started')
    with _exclusive(root, lock_timeout):
        previous = status(root)
        if previous['state'] != 'idle':
            attempt_id = previous['attempt']['attempt_id']
            raise ConversionNeedsReconciliation(f'Word attempt {attempt_id} needs reconciliation; no export was started')
        attempt_id = uuid.uuid4().hex
        attempt_dir = root / attempt_id
        attempt_dir.mkdir(mode=0o700)
        staged_source = attempt_dir / f'{attempt_id}.docx'
        staged_pdf = attempt_dir / f'{attempt_id}.pdf'
        try:
            shutil.copyfile(source, staged_source)
        except BaseException:
            shutil.rmtree(attempt_dir)
            raise
        attempt = {'attempt_id': attempt_id, 'state': 'in_flight', 'started_at': _stamp(),
                   'source': str(source), 'output': str(output), 'staged_source': str(staged_source),
                   'staged_pdf': str(staged_pdf)}
        _atomic_json(root / 'pending.json', attempt)
        try:
            completed = run(['osascript', '-', str(staged_source), str(staged_pdf)], input=WORD_EXPORT,
                            capture_output=True, text=True, check=False, timeout=200)
            if completed.returncode:
                raise WordConversionError(f'Word export exited with code {completed.returncode}')
        except FileNotFoundError:
            (root / 'pending.json').unlink()
            shutil.rmtree(attempt_dir)
            raise WordConversionError('osascript is unavailable; no Word export was started') from None
        except BaseException as exc:
            attempt.update(state='needs_reconciliation', failure=type(exc).__name__, updated_at=_stamp())
            _atomic_json(root / 'pending.json', attempt)
            raise ConversionNeedsReconciliation(
                f'Word attempt {attempt_id} has an uncertain outcome. Preserve its files and reconcile before retrying'
            ) from exc
        try:
            _publish_pdf(staged_pdf, output)
        finally:
            (root / 'pending.json').unlink()
            shutil.rmtree(attempt_dir)
        return output


def reconcile(attempt_id: str, *, evidence: str, confirm_no_in_flight: bool,
              stage_root: Path | None = None) -> dict:
    """Accept an operator's explicit verification that the old Word action has ended."""
    if not confirm_no_in_flight or not evidence.strip():
        raise ValueError('Reconciliation requires evidence and confirmation that no Word action remains in flight')
    root = stage_root or default_stage_root()
    with _exclusive(root, 0):
        current = status(root)
        if current['state'] == 'idle' or current['attempt']['attempt_id'] != attempt_id:
            raise ValueError('Attempt does not match the pending Word conversion')
        attempt = current['attempt']
        attempt.update(state='reconciled', reconciled_at=_stamp(), evidence=evidence.strip())
        archive = root / 'reconciled'
        archive.mkdir(mode=0o700, exist_ok=True)
        _atomic_json(archive / f'{attempt_id}.json', attempt)
        (root / 'pending.json').unlink()
        return {'state': 'reconciled', 'attempt_id': attempt_id, 'files_preserved': True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    convert = commands.add_parser('convert')
    convert.add_argument('source', type=Path)
    convert.add_argument('output', type=Path, nargs='?')
    convert.add_argument('--lock-timeout', type=float, default=30)
    commands.add_parser('status')
    reconcile_parser = commands.add_parser('reconcile')
    reconcile_parser.add_argument('attempt_id')
    reconcile_parser.add_argument('--evidence', required=True)
    reconcile_parser.add_argument('--confirm-no-in-flight', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'convert':
            print(convert_document(args.source, args.output, lock_timeout=args.lock_timeout))
        elif args.command == 'status':
            print(json.dumps(status(), indent=2))
        else:
            print(json.dumps(reconcile(args.attempt_id, evidence=args.evidence,
                                       confirm_no_in_flight=args.confirm_no_in_flight), indent=2))
    except (OSError, ValueError, WordConversionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
