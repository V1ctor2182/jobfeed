"""Submission completion with explicit evidence provenance."""
from __future__ import annotations
import json
from pathlib import Path


def valid_user_submission(data: dict, job_id: int) -> bool:
    return (data.get('job_id') == job_id
            and data.get('status') == 'submitted_user_confirmed'
            and data.get('do_not_reapply') is True
            and isinstance(data.get('user_text'), str) and bool(data['user_text'].strip())
            and isinstance(data.get('recorded_at'), str) and bool(data['recorded_at'].strip()))


def read_user_submission(folder: Path, job_id: int) -> dict:
    try:
        data = json.loads((folder / 'user-submission-confirmation.json').read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and valid_user_submission(data, job_id) else {}
