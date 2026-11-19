"""Read job-bound confirmation artifacts without trusting an agent's success claim."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from jobfeed.applier_trace import _read_only
from jobfeed.domain.normalize import normalize, normalize_company


class EvidenceValidationError(ValueError):
    """A confirmation is missing, unsafe, mismatched, or not an acknowledgement."""


@dataclass(frozen=True)
class SubmitApproval:
    """The user's own Submit approval and the one attempt it allowed.

    The Applier runtime records both: the Dashboard or Discord confirmation
    writes the approval, and the Applier spends it on the attempt right before
    its single Submit click.
    """

    event_id: str
    source: str
    requested_at: datetime
    state: str


ConfirmationKind = Literal[
    "website_screenshot", "confirmation_email", "approved_submit_email"
]


@dataclass(frozen=True)
class ConfirmationEvidence:
    """Validated proof of a submitted application and its capture provenance."""

    kind: ConfirmationKind
    artifact_path: Path
    artifact_bytes: bytes
    media_type: str
    captured_at: str
    source_url: str
    confirmation_text: str
    capture_tool: str
    source_id: str
    email: dict[str, str] | None = None
    approval: SubmitApproval | None = None


_MAX_DIMENSION = 12000
_MAX_PIXELS = 30000000
_MIN_WIDTH = 100
_MIN_HEIGHT = 50
_MAX_FUTURE_SECONDS = 300
# Every confirmation kind: a page capture, an email naming the posting link,
# or an email naming the company that answers the user's approved Submit.
_KINDS = frozenset(
    {"website_screenshot", "confirmation_email", "approved_submit_email"}
)
# ATS acknowledgements arrive within minutes. A day tolerates a delayed mailer
# while keeping the email tied to this attempt rather than an older one.
_APPROVED_EMAIL_WINDOW = timedelta(hours=24)
# The user confirmed Submit in the Dashboard or Discord, or turned on auto submit mode.
_APPROVAL_SOURCES = frozenset({"dashboard", "discord", "auto"})
_ATTEMPTED = frozenset({"requested", "unknown", "submitted"})
# Domains an ATS mails applicants from, keyed by the posting host's domain.
_ATS_MAIL = {
    "ashbyhq.com": frozenset({"ashbyhq.com"}),
    "greenhouse.io": frozenset({"greenhouse.io", "greenhouse-mail.io"}),
    "lever.co": frozenset({"lever.co"}),
    "myworkdayjobs.com": frozenset({"myworkday.com", "myworkdayjobs.com"}),
}

# Workday mails each employer's applicants as <tenant>@myworkday.com,
# whatever board listed the job.
_TENANT_MAIL = frozenset({"myworkday.com"})
_MIN_EMBEDDED_NAME = 5

_ACK = re.compile(
    r"(?:application\s+(?:(?:has\s+been|was|is)\s+)?(?:successfully\s+)?(?:submitted|received|sent)|"
    r"(?:received|submitted)\s+your\s+application|"
    r"thank(?:s|\s+you)(?:\s+so\s+much)?\s+for\s+(?:applying|(?:submitting\s+)?your\s+application|submitting\s+your\s+interest)|"
    r"successfully\s+applied|"
    r"申请(?:已|已经)(?:成功)?(?:提交|收到)|已收到.{0,20}申请)",
    re.I,
)
_NEGATIVE = re.compile(
    r"(?:not|never|unable|failed|couldn.t|cannot|can.t)\s+.{0,35}(?:submit|received)|not\s+(?:been\s+)?(?:submitted|received)|尚未提交|提交失败",
    re.I,
)


def _text(value: object, name: str, limit: int = 16000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise EvidenceValidationError("invalid_" + name)
    return value.strip()


def _url(value: object, name: str) -> str:
    text = _text(value, name, 4096)
    if any(not char.isprintable() or char.isspace() for char in text):
        raise EvidenceValidationError("invalid_" + name)
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise EvidenceValidationError("invalid_" + name) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise EvidenceValidationError("invalid_" + name)
    return text


def _artifact_path(folder: Path, relative: object) -> Path:
    name = _text(relative, "artifact_path", 512)
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise EvidenceValidationError("artifact_outside_application")
    folder = folder.absolute()
    for parent in [folder, *folder.parents]:
        if parent.is_symlink():
            raise EvidenceValidationError("symlink_not_allowed")
        if parent.name == "applications" or (
            parent == folder and "applications" not in folder.parts
        ):
            break
    target = folder / path
    for part in [target, *target.parents]:
        if part == folder:
            break
        if part.is_symlink():
            raise EvidenceValidationError("symlink_not_allowed")
    if not target.resolve().is_relative_to(folder.resolve()):
        raise EvidenceValidationError("artifact_outside_application")
    return target


def _read(folder: Path, relative: object, limit: int) -> tuple[Path, bytes]:
    target = _artifact_path(folder, relative)
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise EvidenceValidationError("artifact_invalid_size_or_type")
            content = stream.read(limit + 1)
        if not content or len(content) > limit:
            raise EvidenceValidationError("artifact_invalid_size_or_type")
        return target, content
    except OSError as exc:
        raise EvidenceValidationError("artifact_missing_or_unreadable") from exc


def _object(folder: Path, name: object) -> dict[str, Any]:
    _, content = _read(folder, name, 256 * 1024)
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise EvidenceValidationError("invalid_artifact_json") from exc
    if not isinstance(value, dict):
        raise EvidenceValidationError("invalid_artifact_json")
    return value


def _image(content: bytes, suffix: str) -> str:
    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format
            if image_format not in {"PNG", "JPEG"} or not (
                _MIN_WIDTH <= image.width <= _MAX_DIMENSION
                and _MIN_HEIGHT <= image.height <= _MAX_DIMENSION
                and image.width * image.height <= _MAX_PIXELS
            ):
                raise EvidenceValidationError("unsupported_screenshot")
            expected = {".png"} if image_format == "PNG" else {".jpg", ".jpeg"}
            if suffix not in expected:
                raise EvidenceValidationError("screenshot_extension_mismatch")
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
        return "image/png" if image_format == "PNG" else "image/jpeg"
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise EvidenceValidationError("invalid_screenshot") from exc


def _html_text(markup: str) -> str:
    """The words a reader sees in an HTML email body."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def _email(raw: bytes, source_id: str, captured: datetime) -> dict[str, str]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if message.defects:
            raise ValueError()
        headers = {
            key: _text(str(message.get(name, "")), key, 2000)
            for key, name in [
                ("sender", "From"),
                ("recipient", "To"),
                ("subject", "Subject"),
                ("message_id", "Message-ID"),
                ("date", "Date"),
            ]
        }
        if headers["message_id"] != source_id or any(
            not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", parseaddr(headers[key])[1])
            for key in ("sender", "recipient")
        ):
            raise ValueError()
        dated = parsedate_to_datetime(headers["date"])
        if dated.tzinfo is None or dated > captured:
            raise ValueError()
        # Some employers, such as Workday tenants, send only an HTML part.
        body = message.get_body(preferencelist=("plain", "html"))
        if body is None:
            raise ValueError()
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = _html_text(content)
        text = _text(content, "email_body", 100000)
    except (ValueError, TypeError, UnicodeError, LookupError) as exc:
        raise EvidenceValidationError("invalid_confirmation_email") from exc
    return {**headers, "body": text}


def _names_posting(email: dict[str, str], expected_url: str) -> None:
    links = re.findall(r"""https?://[^\s<>"']+""", email["body"])
    if not any(
        link == expected_url or link.rstrip(".,;") == expected_url for link in links
    ):
        raise EvidenceValidationError("email_job_link_required")


def _domain(host: str) -> str:
    return ".".join(host.lower().rstrip(".").split(".")[-2:])


def _names_company(text: str, company: str) -> bool:
    words, name = normalize(text).split(), normalize_company(company).split()
    if not name:
        return False
    compact = "".join(name)
    return any(
        words[i : i + len(name)] == name
        or any("".join(words[i : i + n]) == compact for n in (1, 2, 3))
        for i in range(len(words))
    )


def _approval(folder: Path, job_id: int) -> SubmitApproval:
    """Read the job's spent Submit approval from the Applier runtime store.

    The store sits beside `applications/`, which holds every job folder.
    """
    root = next(
        (p.parent for p in folder.absolute().parents if p.name == "applications"),
        None,
    )
    store = root / ".pipeline" / "applier-runtime.sqlite3" if root else None
    if store is None or not store.is_file():
        raise EvidenceValidationError("submit_approval_required")
    try:
        with closing(_read_only(store)) as db:
            attempt = db.execute(
                "SELECT state, requested_at FROM applier_submits WHERE job_id=?",
                (job_id,),
            ).fetchone()
            # Each attempt spends one approval: the latest spent one is this attempt's.
            approval = db.execute(
                "SELECT event_id, source_kind FROM applier_submit_auth "
                "WHERE job_id=? AND used_at IS NOT NULL ORDER BY used_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        requested = datetime.fromisoformat(attempt[1]) if attempt else None
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise EvidenceValidationError("submit_approval_required") from exc
    if (
        attempt is None
        or attempt[0] not in _ATTEMPTED
        or approval is None
        or approval[1] not in _APPROVAL_SOURCES
        or requested is None
        or requested.tzinfo is None
    ):
        raise EvidenceValidationError("submit_approval_required")
    return SubmitApproval(approval[0], approval[1], requested, attempt[0])


def _answers_approval(
    email: dict[str, str],
    approval: SubmitApproval,
    company: str | None,
    expected_url: str,
) -> None:
    """Bind an email without the posting link to the user's approved attempt."""
    if not company or not normalize_company(company):
        raise EvidenceValidationError("job_company_required")
    received = parsedate_to_datetime(email["date"])
    if not (
        approval.requested_at
        <= received
        <= approval.requested_at + _APPROVED_EMAIL_WINDOW
    ):
        raise EvidenceValidationError("email_outside_submit_window")
    mailbox, _, host = parseaddr(email["sender"])[1].lower().rpartition("@")
    sender = _domain(host)
    posting = _domain(urlsplit(expected_url).hostname or "")
    employer = "".join(normalize_company(company).split())
    if (
        sender not in _ATS_MAIL.get(posting, frozenset({posting}))
        and not _employer_domain(sender.split(".")[0], employer)
        and not (sender in _TENANT_MAIL and mailbox == employer)
    ):
        raise EvidenceValidationError("email_sender_not_employer")
    if not _names_company(email["subject"] + "\n" + email["body"], company):
        raise EvidenceValidationError("email_company_required")


def _employer_domain(label: str, employer: str) -> bool:
    """Whether a sender domain's name is the employer's, alone or with a word added.

    For example, makenotion is Notion's. A short name could sit inside an
    unrelated domain, so only names of five or more letters may have a word added.
    """
    return label == employer or (
        len(employer) >= _MIN_EMBEDDED_NAME
        and (label.startswith(employer) or label.endswith(employer))
    )


def _capture_time(value: object) -> tuple[str, datetime]:
    captured_at = _text(value, "captured_at", 100)
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if (
            captured.tzinfo is None
            or (captured.astimezone(UTC) - datetime.now(UTC)).total_seconds()
            > _MAX_FUTURE_SECONDS
        ):
            raise ValueError()
    except ValueError as exc:
        raise EvidenceValidationError("invalid_captured_at") from exc
    return captured_at, captured


def _browser_text(
    folder: Path, confirmation: dict[str, Any], expected: dict[str, Any]
) -> str:
    observation = _object(folder, confirmation.get("observation"))
    if any(observation.get(key) != value for key, value in expected.items()):
        raise EvidenceValidationError("browser_observation_mismatch")
    text = _text(observation.get("text"), "confirmation_text")
    if confirmation.get("text") != text:
        raise EvidenceValidationError("browser_observation_mismatch")
    return text


def read_confirmation(
    folder: Path, job_id: int, job_url: str, *, company: str | None = None
) -> ConfirmationEvidence:
    """Validate persisted evidence against the source posting.

    Tool and message identifiers provide inspectable attribution. They do not
    authenticate a remote employer or establish that captured pixels are genuine.

    Args:
        folder: Application folder holding the submission receipt.
        job_id: Job the receipt must name.
        job_url: Posting URL the receipt must name.
        company: The job's company in Jobfeed. An `approved_submit_email`
            must name it; without it that kind cannot be verified.

    Returns:
        The validated confirmation evidence.

    Raises:
        EvidenceValidationError: If the receipt names another job, lacks an
            artifact or capture provenance, has an unknown kind, lacks the
            original email, or does not acknowledge the application, or when
            an `approved_submit_email` does not answer the user's approved
            Submit attempt for this company.
    """
    receipt = _object(folder, "submission-receipt.json")
    expected_url = _url(job_url, "job_url")
    if (
        type(receipt.get("job_id")) is not int
        or receipt["job_id"] != job_id
        or receipt.get("job_url") != expected_url
    ):
        raise EvidenceValidationError("confirmation_job_mismatch")
    confirmation = receipt.get("confirmation")
    if not isinstance(confirmation, dict):
        raise EvidenceValidationError("confirmation_artifact_required")
    kind = confirmation.get("kind")
    if kind not in _KINDS:
        raise EvidenceValidationError("invalid_confirmation_kind")
    captured_at, captured = _capture_time(confirmation.get("captured_at"))
    source_url = _url(confirmation.get("source_url"), "source_url")
    capture = confirmation.get("capture")
    if not isinstance(capture, dict):
        raise EvidenceValidationError("capture_provenance_required")
    capture_tool = _text(capture.get("tool"), "capture_tool", 200)
    source_id = _text(capture.get("source_id"), "source_id", 500)
    path, raw = _read(folder, confirmation.get("artifact"), 10 * 1024 * 1024)
    email = approval = None
    if kind == "website_screenshot":
        media_type = _image(raw, path.suffix.lower())
        text = _browser_text(
            folder,
            confirmation,
            {
                "job_id": job_id,
                "job_url": expected_url,
                "source_url": source_url,
                "captured_at": captured_at,
                "capture": capture,
            },
        )
    else:
        if path.suffix.lower() != ".eml":
            raise EvidenceValidationError("original_email_required")
        email = _email(raw, source_id, captured)
        if kind == "confirmation_email":
            _names_posting(email, expected_url)
        else:
            approval = _approval(folder, job_id)
            _answers_approval(email, approval, company, expected_url)
        text = email["body"]
        media_type = "message/rfc822"
    if not _ACK.search(text) or _NEGATIVE.search(text):
        raise EvidenceValidationError("application_acknowledgement_missing")
    return ConfirmationEvidence(
        cast(ConfirmationKind, kind),
        path,
        raw,
        media_type,
        captured_at,
        source_url,
        text,
        capture_tool,
        source_id,
        email,
        approval,
    )
