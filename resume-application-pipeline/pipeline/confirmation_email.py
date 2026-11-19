"""Save an original confirmation email as a job's submission evidence, then settle an unknown Submit.

The operator fetches the message's raw RFC 822 bytes from the mail tool. This module never edits them. It keeps the
email only when Jobfeed's own evidence check accepts it, so a rejected email leaves the job folder as it was.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path

from pipeline.applier_runtime import ApplierRuntime

ARTIFACT = 'confirmation.eml'
RECEIPT = 'submission-receipt.json'
MAX_BYTES = 10 * 1024 * 1024
# An email naming the posting link needs nothing else, so it is tried first.
KINDS = ('confirmation_email', 'approved_submit_email')


def _replace(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _restore(path, previous):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _replace(path, previous)


def record(pipeline, job_id, raw, *, source_url, capture_tool):
    """Save `raw` as the job's confirmation if Jobfeed accepts it, and settle an unknown Submit as submitted."""
    from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('the email must be 1 byte to 10 MB of raw RFC 822')
    item = pipeline.queue.get(job_id)
    if item is None or not item.application_folder_relpath:
        raise ValueError(f'job {job_id} has no application folder')
    folder, job = pipeline._folder(item), pipeline.jobfeed.get_job(job_id)
    message_id = str(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True).get('Message-ID', '')).strip()
    if not message_id:
        raise ValueError('the email has no Message-ID header')
    artifact, receipt_path = folder / ARTIFACT, folder / RECEIPT
    saved = artifact.read_bytes() if artifact.exists() else None
    if saved is not None and saved != raw:
        raise ValueError(f'{artifact} already holds a different email')
    previous = receipt_path.read_bytes() if receipt_path.exists() else None
    receipt = json.loads(previous) if previous else {}
    capture = {'tool': capture_tool, 'source_id': message_id}
    captured_at = datetime.now(timezone.utc).isoformat()
    _replace(artifact, raw)
    errors = {}
    for kind in KINDS:
        receipt.update(job_id=job_id, job_url=job.url, confirmation={
            'kind': kind, 'artifact': ARTIFACT, 'captured_at': captured_at, 'source_url': source_url, 'capture': capture})
        _replace(receipt_path, json.dumps(receipt, indent=2, ensure_ascii=False).encode())
        try:
            evidence = read_confirmation(folder, job_id, job.url, company=job.company)
            break
        except EvidenceValidationError as exc:
            errors[kind] = str(exc)
    else:
        _restore(receipt_path, previous)
        _restore(artifact, saved)
        return {'status': 'rejected', 'job_id': job_id, 'errors': errors, 'next': 'Nothing was saved. Never submit again.'}
    return {'status': 'confirmed', 'job_id': job_id, 'kind': evidence.kind, 'artifact': str(artifact), 'message_id': message_id,
            'approval': evidence.approval and {'event_id': evidence.approval.event_id, 'source': evidence.approval.source,
                                               'requested_at': evidence.approval.requested_at.isoformat()},
            'submit': _settle(pipeline.config.resume_root, job_id, evidence.kind, message_id),
            'next': f'Run finish-submission {job_id}.'}


def _settle(root, job_id, kind, message_id):
    """Turn an unknown Applier Submit into submitted. Any other state is left to its Applier."""
    store = ApplierRuntime(root)
    state = store.submit_state(job_id)
    if state is None or state['state'] != 'unknown':
        return {'state': state and state['state'], 'changed': False}
    owner = next((job['owner'] for job in store.jobs() if job['job_id'] == job_id), None)
    if owner is None:
        return {'state': 'unknown', 'changed': False, 'reason': 'no registered Applier owns this job'}
    # The runtime accepts the outcome only from the job's registered Applier identity.
    store.record_submit_outcome(job_id, owner, 'submitted', f'{kind} {message_id} saved by record-confirmation-email')
    return {'state': 'submitted', 'changed': True}
