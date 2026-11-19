"""An email without the posting link counts only for the user's approved Submit."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation

URL = "https://jobs.ashbyhq.com/acme/1"
MESSAGE_ID = "<ack-1@email.amazonses.com>"
REQUESTED = "2026-10-30T15:55:38+00:00"
ACK = (
    "Thank you for taking the time to apply to the Engineer role here at Acme.\n"
    "Please know that your application has been received.\n"
)


def runtime(
    root: Path,
    *,
    state: str = "unknown",
    source: str = "discord",
    used: bool = True,
) -> None:
    store = root / ".pipeline" / "applier-runtime.sqlite3"
    store.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store)) as db, db:
        db.execute(
            "CREATE TABLE applier_submit_auth(event_id TEXT PRIMARY KEY, "
            "job_id INTEGER, source_kind TEXT, recorded_at TEXT, used_at TEXT)"
        )
        db.execute(
            "CREATE TABLE applier_submits(job_id INTEGER PRIMARY KEY, state TEXT, "
            "attempts INTEGER, requested_at TEXT, outcome_at TEXT, evidence TEXT)"
        )
        db.execute(
            "INSERT INTO applier_submit_auth VALUES('approval-1', 1, ?, ?, ?)",
            (source, REQUESTED, REQUESTED if used else None),
        )
        if used:
            db.execute(
                "INSERT INTO applier_submits VALUES(1, ?, 1, ?, NULL, NULL)",
                (state, REQUESTED),
            )


EMAIL = {
    "body": ACK,
    "subject": "Thank You for Applying to Acme",
    "sender": "Acme Hiring Team <no-reply@ashbyhq.com>",
    "date": "Fri, 30 Oct 2026 15:55:50 +0000",
    "content_type": "text/plain",
}


def receipt(root: Path, kind: str = "approved_submit_email", **fields: str) -> Path:
    email = EMAIL | fields
    folder = root / "applications" / "job-1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "confirmation.eml").write_text(
        f"From: {email['sender']}\nTo: applicant@example.net\n"
        f"Subject: {email['subject']}\nMessage-ID: {MESSAGE_ID}\n"
        f"Date: {email['date']}\n"
        f"Content-Type: {email['content_type']}; charset=utf-8\n\n" + email["body"],
        encoding="utf-8",
    )
    confirmation = {
        "kind": kind,
        "artifact": "confirmation.eml",
        "captured_at": "2026-11-01T10:00:00+00:00",
        "source_url": "https://mail.example/messages/1",
        "capture": {"tool": "mail RAW", "source_id": MESSAGE_ID},
    }
    (folder / "submission-receipt.json").write_text(
        json.dumps({"job_id": 1, "job_url": URL, "confirmation": confirmation})
    )
    return folder


def rejection(folder: Path, company: str | None = "acme") -> str:
    with pytest.raises(EvidenceValidationError) as raised:
        read_confirmation(folder, 1, URL, company=company)
    return str(raised.value)


@pytest.mark.parametrize("state", ["requested", "unknown", "submitted"])
def test_an_approved_attempt_accepts_an_email_naming_the_company(
    tmp_path: Path, state: str
) -> None:
    runtime(tmp_path, state=state)
    evidence = read_confirmation(receipt(tmp_path), 1, URL, company="acme")
    assert evidence.kind == "approved_submit_email"
    assert evidence.media_type == "message/rfc822"
    assert evidence.approval is not None
    assert (evidence.approval.event_id, evidence.approval.source) == (
        "approval-1",
        "discord",
    )


@pytest.mark.parametrize("source", ["dashboard", "discord", "auto"])
def test_each_user_confirmation_origin_counts_as_the_approval(
    tmp_path: Path, source: str
) -> None:
    runtime(tmp_path, source=source)
    evidence = read_confirmation(receipt(tmp_path), 1, URL, company="acme")
    assert evidence.approval is not None
    assert evidence.approval.source == source


def test_the_company_may_be_named_only_in_the_subject(tmp_path: Path) -> None:
    runtime(tmp_path)
    body = "Your application has been received.\n"
    folder = receipt(tmp_path, body=body)
    assert read_confirmation(folder, 1, URL, company="Acme, Inc.").email is not None


def test_an_email_with_only_an_html_body_is_read_as_its_visible_text(
    tmp_path: Path,
) -> None:
    runtime(tmp_path)
    body = (
        "<html><head><style>p {color: red}</style></head><body><p>Dear Alex,</p>"
        "<p>We received your application for the Engineer position at Acme &amp; "
        "appreciate your interest.</p></body></html>"
    )
    evidence = read_confirmation(
        receipt(tmp_path, body=body, content_type="text/html"), 1, URL, company="acme"
    )
    assert evidence.confirmation_text.startswith(
        "Dear Alex, We received your application"
    )


def test_an_html_email_saying_it_was_not_submitted_is_refused(tmp_path: Path) -> None:
    runtime(tmp_path)
    body = "<p>Acme: your application was <b>not submitted</b>. Please try again.</p>"
    folder = receipt(tmp_path, body=body, content_type="text/html")
    assert rejection(folder) == "application_acknowledgement_missing"


def test_a_sender_domain_that_adds_a_word_to_the_employer_name_is_accepted(
    tmp_path: Path,
) -> None:
    runtime(tmp_path)
    folder = receipt(
        tmp_path,
        sender="Recruiting <recruiting-no-reply@makeacmeco.com>",
        body="Thank you for applying to Acmeco.\n",
        subject="Thank you for your application to Acmeco",
    )
    assert read_confirmation(folder, 1, URL, company="Acmeco").email is not None


def test_a_short_employer_name_inside_another_domain_is_rejected(
    tmp_path: Path,
) -> None:
    runtime(tmp_path)
    assert (
        rejection(receipt(tmp_path, sender="jobs@dropacme.com"))
        == "email_sender_not_employer"
    )


def test_a_workday_tenant_mailbox_named_for_the_employer_is_accepted(
    tmp_path: Path,
) -> None:
    runtime(tmp_path)
    folder = receipt(tmp_path, sender="acme@myworkday.com")
    assert read_confirmation(folder, 1, URL, company="Acme").email is not None


def test_a_workday_mailbox_of_another_tenant_is_rejected(tmp_path: Path) -> None:
    runtime(tmp_path)
    assert (
        rejection(receipt(tmp_path, sender="globex@myworkday.com"))
        == "email_sender_not_employer"
    )


def test_an_employer_domain_sender_is_accepted(tmp_path: Path) -> None:
    runtime(tmp_path)
    folder = receipt(tmp_path, sender="Recruiting <jobs@acme.ai>")
    assert read_confirmation(folder, 1, URL, company="acme").approval is not None


def test_no_runtime_store_means_no_approval(tmp_path: Path) -> None:
    assert rejection(receipt(tmp_path)) == "submit_approval_required"


@pytest.mark.parametrize(
    "setup",
    [
        {"used": False},
        {"state": "not_submitted"},
        {"source": "test"},
    ],
    ids=["approval_never_spent", "attempt_proved_unsent", "not_the_user"],
)
def test_an_email_needs_the_users_spent_approval_for_an_attempt(
    tmp_path: Path, setup: dict
) -> None:
    runtime(tmp_path, **setup)
    assert rejection(receipt(tmp_path)) == "submit_approval_required"


def test_the_email_must_name_the_company(tmp_path: Path) -> None:
    runtime(tmp_path)
    folder = receipt(
        tmp_path,
        subject="Thank you for applying",
        body="Your application has been received.\n",
    )
    assert rejection(folder) == "email_company_required"


def test_a_similar_company_name_is_not_a_match(tmp_path: Path) -> None:
    runtime(tmp_path)
    assert rejection(receipt(tmp_path), company="acme robotics") == (
        "email_company_required"
    )


def test_the_job_company_is_required(tmp_path: Path) -> None:
    runtime(tmp_path)
    assert rejection(receipt(tmp_path), company=None) == "job_company_required"


@pytest.mark.parametrize(
    "date",
    ["Fri, 30 Oct 2026 15:55:00 +0000", "Sat, 31 Oct 2026 16:00:00 +0000"],
    ids=["before_submit", "a_day_after_submit"],
)
def test_the_email_must_arrive_soon_after_submit(tmp_path: Path, date: str) -> None:
    runtime(tmp_path)
    assert rejection(receipt(tmp_path, date=date)) == "email_outside_submit_window"


def test_an_unrelated_sender_is_rejected(tmp_path: Path) -> None:
    runtime(tmp_path)
    folder = receipt(tmp_path, sender="Alerts <jobs@jobboard.example>")
    assert rejection(folder) == "email_sender_not_employer"


def test_a_negative_email_is_not_an_acknowledgement(tmp_path: Path) -> None:
    runtime(tmp_path)
    body = "Your application to Acme was not submitted. Please try again.\n"
    assert rejection(receipt(tmp_path, body=body)) == (
        "application_acknowledgement_missing"
    )


def test_a_link_bound_email_still_needs_the_posting_link(tmp_path: Path) -> None:
    runtime(tmp_path)
    folder = receipt(tmp_path, kind="confirmation_email")
    assert rejection(folder) == "email_job_link_required"


def test_a_link_bound_email_needs_no_approval(tmp_path: Path) -> None:
    folder = receipt(tmp_path, kind="confirmation_email", body=ACK + URL + "\n")
    evidence = read_confirmation(folder, 1, URL)
    assert (evidence.kind, evidence.approval) == ("confirmation_email", None)
