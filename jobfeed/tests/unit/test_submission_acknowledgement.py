"""Which confirmation page texts count as an employer's acknowledgement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation
from tests.web.test_confirmation_evidence import PNG

URL = "https://jobs.example.test/acme/1"
QUORA = (
    "Overview\nApplication\nSuccess\n\n"
    "Thanks so much for applying to join us at Quora! "
    "Our hiring managers will review your application "
    "and get in touch if there is a fit. "
    "\U0001f603\n\n"
    "Powered by\n\nPrivacy PolicySecurityVulnerability Disclosure"
)


def screenshot_receipt(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    capture = {"tool": "browser screenshot", "source_id": "tab 7 tool_use 1"}
    observation = {
        "job_id": 1,
        "job_url": URL,
        "captured_at": "2026-10-31T03:07:46+00:00",
        "source_url": URL + "/application",
        "text": text,
        "capture": capture,
    }
    (folder / "confirmation.png").write_bytes(PNG)
    (folder / "confirmation-page.json").write_text(json.dumps(observation))
    confirmation = dict(
        observation,
        kind="website_screenshot",
        artifact="confirmation.png",
        observation="confirmation-page.json",
    )
    for key in ("job_id", "job_url"):
        confirmation.pop(key)
    (folder / "submission-receipt.json").write_text(
        json.dumps({"job_id": 1, "job_url": URL, "confirmation": confirmation})
    )
    return folder


@pytest.mark.parametrize(
    "text",
    [
        QUORA,
        "Thanks for applying! We'll be in touch.",
        "Thank you for your application to Acme.",
        (
            "Success\nThanks for submitting your application! "
            "We'll be in touch in a couple days"
        ),
        (
            "Success\nYour application was successfully submitted. "
            "We'll contact you if there are next steps."
        ),
        (
            "Confirmation\nExit to job board\n"
            "You have successfully applied to Software Engineer, New Grad"
        ),
        (
            "We received your application. "
            "Emails not related to accommodation requests will not receive responses."
        ),
        (
            "My Applications\nThank you for submitting your interest to a position "
            "with PNC. As you move through the recruiting process, "
            "you will be able to see the status of your application below."
        ),
    ],
)
def test_an_employer_acknowledgement_is_accepted(tmp_path: Path, text: str) -> None:
    evidence = read_confirmation(screenshot_receipt(tmp_path / "job", text), 1, URL)
    assert evidence.confirmation_text == text


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for your interest in Acme. Submit Application",
        (
            "Thanks for applying, but your application was not submitted. "
            "Fix the errors below."
        ),
        "Application\nFull Name\nEmail\nSubmit Application",
        "You have not successfully applied yet. Your application was not submitted.",
        "Thanks for applying. We have not yet received your resume.",
    ],
)
def test_a_page_without_an_acknowledgement_is_refused(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(
        EvidenceValidationError, match="application_acknowledgement_missing"
    ):
        read_confirmation(screenshot_receipt(tmp_path / "job", text), 1, URL)
