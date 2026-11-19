"""Synthetic confirmation artifacts for submission boundary tests."""

from __future__ import annotations

import json
from pathlib import Path


def confirmation_folder(
    folder: Path, job_id: int = 42, job_url: str = "https://careers.example/jobs/42"
) -> Path:
    """Write a synthetic confirmation email and receipt for one job.

    Args:
        folder: Application folder that receives the evidence files.
        job_id: Job ID the receipt is bound to.
        job_url: Posting URL the receipt and email are bound to.

    Returns:
        The folder holding the saved evidence.
    """
    folder.mkdir(parents=True, exist_ok=True)
    message_id = "<application-42@careers.example>"
    (folder / "confirmation.eml").write_text(
        "From: careers@careers.example\n"
        "To: applicant@example.net\n"
        "Date: Wed, 21 Oct 2026 10:00:00 +0000\n"
        "Subject: Application received\n"
        f"Message-ID: {message_id}\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        f"Application received for {job_url}\n",
        encoding="utf-8",
    )
    (folder / "submission-receipt.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "job_url": job_url,
                "confirmation": {
                    "kind": "confirmation_email",
                    "artifact": "confirmation.eml",
                    "captured_at": "2026-10-21T10:01:00+00:00",
                    "source_url": "https://mail.example/messages/42",
                    "capture": {"tool": "synthetic-mail-test", "source_id": message_id},
                },
            }
        ),
        encoding="utf-8",
    )
    return folder
