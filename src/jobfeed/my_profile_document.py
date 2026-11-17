"""Lossless edits to the existing application profile's known public fields."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from pydantic import ValidationError

from jobfeed.my_profile_models import PersonalDetails, PersonalProfileState
from jobfeed.onboarding_resume_files import _write_private_bytes, private_file_lock

_FIELDS = {
    "legal_name": ("Identity and contact", "Legal name"),
    "preferred_name": ("Identity and contact", "Preferred name"),
    "primary_email": ("Identity and contact", "Primary email"),
    "phone": ("Identity and contact", "Phone"),
    "address": ("Identity and contact", "Address"),
    "linkedin_url": ("Identity and contact", "LinkedIn"),
    "portfolio_url": ("Identity and contact", "Portfolio"),
    "github_url": ("Identity and contact", "GitHub"),
    "work_authorization": ("Eligibility and preferences", "Work authorization"),
    "sponsorship_requirement": (
        "Eligibility and preferences",
        "Sponsorship requirement",
    ),
    "relocation": ("Eligibility and preferences", "Relocation"),
    "availability": ("Eligibility and preferences", "Availability"),
    "education": ("Eligibility and preferences", "Education and dates"),
}
_LABELS = {
    (section.casefold(), label.casefold()): key
    for key, (section, label) in _FIELDS.items()
}
_NAMES = {
    name.casefold(): key for key, (_, label) in _FIELDS.items() for name in (key, label)
} | {
    "email": "primary_email",
    "email address": "primary_email",
    "e-mail": "primary_email",
    "phone number": "phone",
    "mobile phone": "phone",
    "mobile": "phone",
}
_NAME_SUFFIXES = (" url", " link", " profile")
_LEARNED_SECTION = "Identity and contact"
_CREDENTIAL = re.compile(
    r"pass(word|code)|\botp\b|secret|credential|account|\bssn\b|social security",
    re.IGNORECASE,
)
_NON_ANSWERS = frozenset(
    {
        "yes",
        "no",
        "ok",
        "okay",
        "keep",
        "done",
        "skip",
        "none",
        "na",
        "null",
        "idk",
        "same",
        "blank",
        "leaveblank",
    }
)
_MAX_LABEL = 40
_MAX_VALUE = 200
_MIN_PHONE_DIGITS = 7
_MAX_NAME_WORDS = 6
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^([ \t]*[-*][ \t]+)([^:]+)(:[ \t]*)(.*?)(\r?\n)?$")
_OLD_UNKNOWN = (
    "Work authorization, sponsorship, address, relocation, availability, "
    "preferred name, "
    "and account credentials are not recorded. Do not infer answers. "
    "Check each required "
    "field before an application. This profile grants no application "
    "or submission authority."
)
_NEW_UNKNOWN = (
    "Blank personal fields remain unknown. Do not infer answers. Check each required "
    "field before an application. This profile grants no application "
    "or submission authority."
)


class ProfileConflict(ValueError):
    """The document changed or contains ambiguous editable fields."""


def _revision(path: Path) -> str:
    if not path.exists():
        return "missing"
    stat = path.stat()
    return f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"


def _document_fields(lines: list[str]) -> dict[str, tuple[int, re.Match[str]]]:
    section = ""
    found: dict[str, tuple[int, re.Match[str]]] = {}
    for index, line in enumerate(lines):
        heading = _HEADING.match(line.rstrip("\r\n"))
        if heading:
            section = heading[2].casefold() if heading[1] == "##" else ""
            continue
        match = _FIELD.match(line)
        key = _LABELS.get((section, match[2].strip().casefold())) if match else None
        if key and match:
            if key in found:
                raise ProfileConflict(
                    "The profile contains repeated personal fields. "
                    "Resolve them in the profile file before saving."
                )
            found[key] = (index, match)
    return found


def _blank(value: str) -> bool:
    return not value.strip() or value.strip().startswith("[confirm")


def _with_value(match: re.Match[str], value: str) -> str:
    filling = value and not match[4] and match[3] == ":"
    separator = ": " if filling else match[3]
    return f"{match[1]}{match[2]}{separator}{value}{match[5] or ''}"


def _details(lines: list[str]) -> PersonalDetails:
    values = {
        key: "" if _blank(match[4]) else match[4]
        for key, (_, match) in _document_fields(lines).items()
    }
    return PersonalDetails.model_validate(values)


def _validated(values: dict[str, str]) -> PersonalDetails:
    try:
        return PersonalDetails.model_validate(values)
    except ValidationError as exc:
        reason = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise ValueError(reason) from exc


def _detail_rule(key: str, value: str) -> str:
    """Hold learned links and emails to the same rules as the dashboard's fields."""
    return str(getattr(_validated({key: value}), key))


def _phone_rule(value: str) -> str:
    digits = sum(character.isdigit() for character in value)
    if digits < _MIN_PHONE_DIGITS or not re.fullmatch(r"[\d\s()+.-]+", value):
        raise ValueError("Enter a phone number with at least 7 digits")
    return value


def _name_rule(value: str) -> str:
    words = value.split()
    if (
        not 1 <= len(words) <= _MAX_NAME_WORDS
        or not any(character.isalpha() for character in value)
        or not all(character.isalpha() or character in " -'." for character in value)
    ):
        raise ValueError("Enter a name of one to six words")
    return value


_VALUE_RULES = (
    (
        ("url", "link", "website", "github", "linkedin", "portfolio", "gitlab"),
        partial(_detail_rule, "github_url"),
    ),
    (("email",), partial(_detail_rule, "primary_email")),
    (("phone", "mobile"), _phone_rule),
    (("name",), _name_rule),
)


def _fact_value(label: str, value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > _MAX_VALUE
        or any(character in value for character in "\n\r\x00")
    ):
        raise ValueError("Enter one line of at most 200 characters")
    if re.sub(r"[\W_]+", "", value.casefold()) in _NON_ANSWERS:
        raise ValueError("The reply does not give a value for this field")
    folded = label.casefold()
    rule = next(
        (rule for words, rule in _VALUE_RULES if any(w in folded for w in words)),
        None,
    )
    return rule(value) if rule else value


def _folded(label: str) -> str:
    return " ".join(label.casefold().split())


def _known_field(label: str) -> str | None:
    name = _folded(label)
    names = [name] + [name.removesuffix(s) for s in _NAME_SUFFIXES if name.endswith(s)]
    return next((_NAMES[n] for n in names if n in _NAMES), None)


def _learned_label(label: str) -> str:
    label = label.strip()
    if not (
        1 <= len(label) <= _MAX_LABEL
        and label[0].isalpha()
        and all(character.isalnum() or character in " -'/" for character in label)
    ):
        raise ValueError("Name the field in at most 40 letters, digits and spaces")
    if _CREDENTIAL.search(label):
        raise ValueError("Credentials are never saved to My Profile")
    return label


def _labelled(lines: list[str], label: str) -> list[tuple[int, re.Match[str]]]:
    wanted = _folded(label)
    return [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _FIELD.match(line)) and _folded(match[2]) == wanted
    ]


def _add_fields(lines: list[str], additions: dict[str, list[str]]) -> list[str]:
    for section, fields in additions.items():
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if line.strip().casefold() == f"## {section}".casefold()
            ),
            None,
        )
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend([f"\n## {section}\n", *fields])
        else:
            if not lines[start].endswith("\n"):
                lines[start] += "\n"
            lines[start + 1 : start + 1] = fields
    return lines


class PersonalProfileStore:
    """Preserve unknown prose, credentials and scoped answers during edits."""

    def __init__(self, profile: Path, review: Path) -> None:
        self.profile = profile
        self.review = review

    def state(self) -> PersonalProfileState:
        """Read only the reviewed field projection, without returning raw Markdown.

        Returns:
            The personal details, their revision and whether they were reviewed.

        Raises:
            ProfileConflict: If the profile changed while it was being read.
        """
        before = _revision(self.profile)
        content = self.profile.read_bytes() if self.profile.exists() else b""
        if before != _revision(self.profile):
            raise ProfileConflict(
                "The profile changed while loading. Reload My Profile."
            )
        details = _details(content.decode("utf-8").splitlines(keepends=True))
        reviewed = False
        if self.review.exists():
            try:
                snapshot = json.loads(self.review.read_text())
                reviewed = snapshot.get("personal_details") == details.model_dump()
            except (ValueError, AttributeError):
                reviewed = False
        return PersonalProfileState(
            personal_details=details, revision=before, reviewed=reviewed
        )

    def save(self, details: PersonalDetails, revision: str) -> PersonalProfileState:
        """Reject stale edits, patch known fields, and record explicit review.

        Args:
            details: Personal details to write.
            revision: Profile revision the edit was based on.

        Returns:
            The saved personal profile state.

        Raises:
            ProfileConflict: If the profile changed since `revision` or during the
                save.
        """
        with private_file_lock(self.review.with_suffix(".lock")):
            if revision != _revision(self.profile):
                raise ProfileConflict(
                    "Your profile changed in another window. Reload before saving."
                )
            self._write(details, revision)
            snapshot = {
                "personal_details": details.model_dump(),
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
            _write_private_bytes(
                self.review,
                (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode(),
            )
            return self.state()

    def remember(self, label: str, value: str) -> bool:
        """Save one fact from the user's own reply, without marking review.

        A label naming a known field fills that field. Any other label keeps the
        fact as its own line under Identity and contact.

        Args:
            label: The field's label in plain words, such as Full name or GitHub.
            value: The user's reply.

        Returns:
            Whether the fact was saved. A field that already has a value is kept.

        Raises:
            ValueError: If the label or value cannot be saved, with the reason.
            ProfileConflict: If the profile changed during the save.
        """
        with private_file_lock(self.review.with_suffix(".lock")):
            current = self.state()
            if key := _known_field(label):
                section, field_label = _FIELDS[key]
                if section != _LEARNED_SECTION:
                    # Yes/no preferences such as relocation come from My Profile's
                    # common questions, where "No" is a real answer, not a non-answer.
                    raise ValueError(f"{field_label} is set in My Profile, not here")
                value = _fact_value(field_label, value)
                if getattr(current.personal_details, key):
                    return False
                values = {**current.personal_details.model_dump(), key: value}
                self._write(_validated(values), current.revision)
                return True
            label = _learned_label(label)
            value = _fact_value(label, value)
            lines = self._lines()
            existing = _labelled(lines, label)
            if any(not _blank(match[4]) for _, match in existing):
                return False
            if existing:
                index, match = existing[0]
                lines[index] = _with_value(match, value)
            else:
                lines = _add_fields(
                    lines, {_LEARNED_SECTION: [f"- {label}: {value}\n"]}
                )
            self._commit(lines, current.revision)
            return True

    def _write(self, details: PersonalDetails, revision: str) -> None:
        lines = self._lines()
        found = _document_fields(lines)
        additions: dict[str, list[str]] = {}
        for key, value in details.model_dump().items():
            section, label = _FIELDS[key]
            if key in found:
                index, match = found[key]
                lines[index] = _with_value(match, value)
            elif value:
                additions.setdefault(section, []).append(f"- {label}: {value}\n")
        self._commit(_add_fields(lines, additions), revision)

    def _lines(self) -> list[str]:
        original = (
            self.profile.read_bytes()
            if self.profile.exists()
            else b"# Application profile\n"
        )
        return original.decode("utf-8").splitlines(keepends=True)

    def _commit(self, lines: list[str], revision: str) -> None:
        updated = "".join(lines).replace(_OLD_UNKNOWN, _NEW_UNKNOWN)
        if revision != _revision(self.profile):
            raise ProfileConflict(
                "Your profile changed while saving. Reload before saving."
            )
        _write_private_bytes(self.profile, updated.encode())
