"""Browser-safe personal details and application résumé selection."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PersonalDetails(BaseModel):
    """Explicit facts; empty strings represent information not supplied."""

    model_config = ConfigDict(extra="forbid", str_max_length=2000)
    legal_name: str = ""
    preferred_name: str = ""
    primary_email: str = ""
    phone: str = ""
    address: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    github_url: str = ""
    work_authorization: str = ""
    sponsorship_requirement: str = ""
    relocation: str = ""
    availability: str = ""
    education: str = ""

    @field_validator("*")
    @classmethod
    def single_line(cls, value: str) -> str:
        """Prevent values from creating Markdown structure or scoped answers.

        Args:
            value: Submitted field value.

        Returns:
            The value with surrounding whitespace removed.

        Raises:
            ValueError: If the value contains a newline, carriage return or NUL.
        """
        if any(character in value for character in "\n\r\x00"):
            raise ValueError("Personal details must contain one line per field")
        return value.strip()

    @field_validator("primary_email")
    @classmethod
    def email_format(cls, value: str) -> str:
        """Check a nonempty address without requiring network or extra packages.

        Args:
            value: Submitted email address, possibly empty.

        Returns:
            The value unchanged.

        Raises:
            ValueError: If a nonempty value is not shaped like an email address.
        """
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter an email address such as name@example.com")
        return value

    @field_validator("linkedin_url", "portfolio_url", "github_url")
    @classmethod
    def website_format(cls, value: str) -> str:
        """Accept only explicit public web URL syntax.

        Args:
            value: Submitted website URL, possibly empty.

        Returns:
            The value unchanged.

        Raises:
            ValueError: If a nonempty value is not an HTTP(S) URL with a host and no
                credentials, or contains whitespace.
        """
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            valid = False
        if not valid or any(character.isspace() for character in value):
            raise ValueError("Enter a complete http:// or https:// website URL")
        return value


class PersonalProfileState(BaseModel):
    """Editable projection and a concurrency token without private notes."""

    personal_details: PersonalDetails
    revision: str
    reviewed: bool


class ApplicationResume(BaseModel):
    """Display metadata only, never local filesystem paths."""

    id: str
    label: str
    filename: str
    available: bool


class UploadedResume(BaseModel):
    """One filename in the private upload library."""

    id: str
    filename: str
    active: bool
    format: str
    application_supported: bool
    status: str
    kind: str = "legacy"
    label: str = ""
    role: str = ""
    google_doc_url: str = ""
    page_count: int | None = None
    pdf_export: dict[str, Any] | None = None
    download_url: str = ""


class ResumeActiveUpdate(BaseModel):
    """Request body that turns one uploaded résumé on or off."""

    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool


class MyProfileState(PersonalProfileState):
    """Combined view of personal facts and application résumé selection."""

    application_resumes: list[ApplicationResume] = Field(default_factory=list)
    resumes: list[UploadedResume] = Field(default_factory=list)


class PersonalDetailsUpdate(BaseModel):
    """A complete personal-details form bound to its loaded document."""

    model_config = ConfigDict(extra="forbid")
    personal_details: PersonalDetails
    revision: str = Field(max_length=120)


class ResumeAvailabilityUpdate(BaseModel):
    """Explicit availability for one application résumé."""

    model_config = ConfigDict(extra="forbid", strict=True)
    available: bool


class AddCurrentResume(BaseModel):
    """Only the server's current scoring upload may be copied."""

    model_config = ConfigDict(extra="forbid")
