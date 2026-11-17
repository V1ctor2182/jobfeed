"""Master material, application variants, and recorded PDF exports."""

import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ResumeUploadMetadata(BaseModel):
    """User-supplied details for an uploaded résumé."""

    model_config = ConfigDict(extra="forbid", str_max_length=1000)

    kind: Literal["legacy", "master", "variant"] = "legacy"
    label: str = ""
    role: str = ""
    google_doc_url: str = ""
    exported_at: datetime | None = None
    provenance: Literal["user_declared", "observed_google_docs_export"] | None = None

    @field_validator("label", "role")
    @classmethod
    def single_line(cls, value: str) -> str:
        """Reject control characters in labels and roles.

        Args:
            value: Submitted label or role.

        Returns:
            The value with surrounding whitespace removed.

        Raises:
            ValueError: If the value contains a control character.
        """
        if any(character < " " for character in value):
            raise ValueError("Resume labels and roles must be single lines")
        return value.strip()

    @field_validator("google_doc_url")
    @classmethod
    def document_url(cls, value: str) -> str:
        """Normalize a Google Docs URL to its `/edit` form.

        Args:
            value: Submitted Google Docs URL, possibly empty.

        Returns:
            The empty string, or the document's canonical edit URL.

        Raises:
            ValueError: If a nonempty value is not a Google Docs document URL.
        """
        value = value.strip()
        if not value:
            return value
        match = re.fullmatch(
            r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)(?:/[^\s]*)?",
            value,
        )
        if not match:
            raise ValueError("Enter a Google Docs document URL")
        return f"https://docs.google.com/document/d/{match[1]}/edit"

    @model_validator(mode="after")
    def consistent_export(self) -> Self:
        """Check that the kind, role, Google Docs URL and export fields agree.

        Returns:
            The validated model.

        Raises:
            ValueError: If a variant lacks a role, export provenance lacks a variant
                URL, an export time lacks provenance, or an export time has no zone.
        """
        if self.kind == "variant" and not self.role:
            raise ValueError("An application variant needs a target role")
        if self.provenance and (self.kind != "variant" or not self.google_doc_url):
            raise ValueError("A PDF export needs its variant's Google Docs URL")
        if self.exported_at and not self.provenance:
            raise ValueError("An export time needs export provenance")
        if self.exported_at and self.exported_at.utcoffset() is None:
            raise ValueError("The export time must include its timezone")
        return self


def variant_export_matches(row: dict[str, Any]) -> bool:
    """Check that a library row is a one-page variant with a matching export.

    Args:
        row: Library entry to check.

    Returns:
        True when the recorded export belongs to the row's stored PDF and Google
        Docs URL and both are one page.
    """
    export = row.get("pdf_export") or {}
    if not isinstance(export, dict):
        return False
    return bool(
        row.get("kind") == "variant"
        and row.get("role")
        and row.get("google_doc_url")
        and row.get("page_count") == 1
        and export.get("stored_name") == row.get("stored_name")
        and export.get("source_google_doc_url") == row.get("google_doc_url")
        and export.get("page_count") == 1
        and export.get("provenance") in {"user_declared", "observed_google_docs_export"}
    )
