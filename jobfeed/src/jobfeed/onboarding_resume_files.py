"""Validate, extract, and privately persist original résumé uploads."""

from __future__ import annotations

import fcntl
import io
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from docx import Document
from pypdf import PdfReader

from jobfeed.onboarding_resume_types import StoredResume
from jobfeed.resume_library_types import ResumeUploadMetadata, variant_export_matches

_ALLOWED_EXTENSIONS = frozenset({".pdf", ".docx", ".md", ".txt"})
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ResumeFileStore:
    """Validate, extract, and privately persist original résumé uploads."""

    def __init__(self, root: Path, max_bytes: int = _MAX_UPLOAD_BYTES) -> None:
        """Create a store constrained to ``data/resumes``."""
        self._root = root.resolve()
        self._max_bytes = max_bytes

    def save(  # noqa: C901, PLR0913 - validates every upload field before one atomic write
        self,
        filename: str,
        content: bytes,
        *,
        known_names: dict[str, str] | None = None,
        known_active: dict[str, bool] | None = None,
        resume_metadata: ResumeUploadMetadata | None = None,
        identifier: str | None = None,
    ) -> StoredResume:
        """Validate, extract, and atomically save one original résumé.

        Args:
            filename: Browser-supplied original filename.
            content: Uploaded bytes.

        Returns:
            Stored path and locally extracted text.

        Raises:
            ValueError: If the type, size, encoding, or text is invalid.
        """
        original_name = _safe_filename(filename)
        extension = Path(original_name).suffix.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise ValueError("Upload a PDF, DOCX, Markdown, or plain text résumé")
        if not content:
            raise ValueError("The résumé file is empty")
        if len(content) > self._max_bytes:
            raise ValueError("The résumé file must be 10 MB or smaller")
        extracted_text = _extract_text(extension, content).strip()
        if not extracted_text:
            raise ValueError("The résumé contains no extractable text")
        metadata = resume_metadata or ResumeUploadMetadata()
        page_count = (
            len(PdfReader(io.BytesIO(content)).pages) if extension == ".pdf" else None
        )
        if metadata.kind == "variant" and (extension != ".pdf" or page_count != 1):
            raise ValueError("Application variants require a one-page text PDF")

        stored_name = f"{uuid4().hex}{extension}"
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / stored_name
        with private_file_lock(self._root / ".library.lock"):
            library = self._library(known_names, known_active)
            same_name = next(
                (
                    row
                    for row in library["entries"]
                    if row["original_name"].casefold() == original_name.casefold()
                ),
                None,
            )
            if same_name and same_name.get("kind", "legacy") != metadata.kind:
                raise ValueError(
                    "Use a different filename for the master and each "
                    "application variant"
                )
            if metadata.kind == "variant" and same_name and not identifier:
                raise ValueError(
                    "This variant filename already exists. Use Edit or replace, "
                    "or choose a different filename"
                )
            existing = (
                next(
                    (row for row in library["entries"] if row["id"] == identifier), None
                )
                if identifier
                else same_name
            )
            if identifier and existing is None:
                raise ValueError("This resume no longer exists")
            if existing and existing.get("kind", "legacy") != metadata.kind:
                raise ValueError("Replace a resume using its existing purpose")
            if metadata.kind == "master":
                master = next(
                    (row for row in library["entries"] if row.get("kind") == "master"),
                    None,
                )
                if master:
                    existing = master
            if same_name and existing and same_name["id"] != existing["id"]:
                raise ValueError("This filename belongs to another resume")
            _write_private_bytes(path, content)
            replacement = {
                **(existing or {}),
                "id": existing["id"] if existing else f"upload-{uuid4().hex}",
                "original_name": original_name,
                "stored_name": stored_name,
                "active": existing["active"] if existing else True,
                "active_explicit": True,
                "uploaded_at": datetime.now(UTC).isoformat(),
                "kind": metadata.kind,
                "label": metadata.label
                or (existing or {}).get("label")
                or original_name,
                "role": metadata.role,
                "google_doc_url": metadata.google_doc_url,
                "page_count": page_count,
                "pdf_export": None,
            }
            if metadata.provenance:
                replacement["pdf_export"] = {
                    "stored_name": stored_name,
                    "source_google_doc_url": metadata.google_doc_url,
                    "exported_at": metadata.exported_at.isoformat()
                    if metadata.exported_at
                    else None,
                    "page_count": page_count,
                    "provenance": metadata.provenance,
                }
            if metadata.kind == "master" or (
                metadata.kind == "variant" and not variant_export_matches(replacement)
            ):
                replacement["active"] = False
            library["entries"] = [
                row for row in library["entries"] if row is not existing
            ] + [replacement]
            if existing:
                library["retired_files"].append(existing["stored_name"])
            self._publish_library(library)
        return StoredResume(
            path=path,
            stored_name=stored_name,
            original_name=original_name,
            extracted_text=extracted_text,
        )

    def _library(  # noqa: C901 - repairs each legacy library shape in one read
        self,
        known_names: dict[str, str] | None = None,
        known_active: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        path = self._root / "library.json"
        library: dict[str, Any] = (
            json.loads(path.read_text())
            if path.exists()
            else {"schema_version": 1, "entries": [], "retired_files": []}
        )
        if library.get("schema_version") != 1 or not isinstance(
            library.get("entries"), list
        ):
            raise ValueError("The uploaded resume library needs repair.")
        known_names = known_names or {}
        known_active = known_active or {}
        rows = []
        retired = library.get("retired_files", [])
        if not isinstance(retired, list) or not all(
            isinstance(name, str) and Path(name).name == name for name in retired
        ):
            raise ValueError("Invalid uploaded resume history.")
        represented = set(retired)
        for entry in library["entries"]:
            if (
                not isinstance(entry, dict)
                or not all(
                    isinstance(entry.get(k), str)
                    for k in ("id", "original_name", "stored_name", "uploaded_at")
                )
                or type(entry.get("active")) is not bool
            ):
                raise ValueError("The uploaded resume library has invalid entries.")
            name = entry["stored_name"]
            if Path(name).name != name:
                raise ValueError("Invalid uploaded resume path.")
            row = dict(entry)
            row["original_name"] = known_names.get(name, row["original_name"])
            if (
                not row.get("active_explicit")
                and row["original_name"].casefold() in known_active
            ):
                row["active"] = known_active[row["original_name"].casefold()]
                row["active_explicit"] = True
            rows.append(row)
            represented.add(name)
        # Original filenames are recovered only from recorded metadata. Unknown
        # legacy files retain their actual basename rather than an invented name.
        for source in sorted(self._root.glob("*")):
            if (
                source.is_symlink()
                or not source.is_file()
                or source.suffix.lower() not in _ALLOWED_EXTENSIONS
                or source.name in represented
            ):
                continue
            rows.append(
                {
                    "id": f"upload-{uuid4().hex}",
                    "original_name": known_names.get(source.name, source.name),
                    "stored_name": source.name,
                    "active": known_active.get(
                        known_names.get(source.name, source.name).casefold(), True
                    ),
                    "active_explicit": known_names.get(
                        source.name, source.name
                    ).casefold()
                    in known_active,
                    "uploaded_at": datetime.fromtimestamp(
                        source.stat().st_mtime, UTC
                    ).isoformat(),
                }
            )
        by_name: dict[str, dict[str, Any]] = {}
        for row in sorted(rows, key=lambda row: row["uploaded_at"]):
            key = row["original_name"].casefold()
            previous = by_name.get(key)
            if previous:
                row["id"] = previous["id"]
                retired.append(previous["stored_name"])
            by_name[key] = row
        return {
            "schema_version": 1,
            "entries": list(by_name.values()),
            "retired_files": list(dict.fromkeys(retired)),
        }

    def _publish_library(self, library: dict[str, Any]) -> None:
        _write_private_bytes(
            self._root / "library.json",
            (json.dumps(library, ensure_ascii=False, indent=2) + "\n").encode(),
        )

    def library(
        self,
        known_names: dict[str, str] | None = None,
        known_active: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Load the upload library, adopting untracked files, and publish it.

        Args:
            known_names: Display names to use for stored files, by stored name.
            known_active: Active choices to carry over, by casefolded filename.

        Returns:
            The library entries.
        """
        with private_file_lock(self._root / ".library.lock"):
            library = self._library(known_names, known_active)
            self._publish_library(library)
            entries: list[dict[str, Any]] = library["entries"]
            return entries

    def set_active(
        self, identifier: str, active: bool, known_names: dict[str, str] | None = None
    ) -> None:
        """Turn one uploaded résumé on or off and mark the choice explicit.

        Args:
            identifier: Library identifier of the upload.
            active: Whether the upload should be active.
            known_names: Display names to use for stored files, by stored name.

        Raises:
            ValueError: If the upload no longer exists, or activation is asked for a
                master or a variant without a matching one-page export.
        """
        with private_file_lock(self._root / ".library.lock"):
            library = self._library(known_names)
            row = next(
                (row for row in library["entries"] if row["id"] == identifier), None
            )
            if row is None:
                raise ValueError("This uploaded resume no longer exists.")
            if active and (
                row.get("kind") == "master"
                or (row.get("kind") == "variant" and not variant_export_matches(row))
            ):
                raise ValueError(
                    "Only a one-page variant with a recorded Google Docs export "
                    "can be activated"
                )
            row["active"] = active
            row["active_explicit"] = True
            self._publish_library(library)

    def update_metadata(self, identifier: str, metadata: ResumeUploadMetadata) -> None:
        """Update an upload's label, role and Google Docs URL.

        Changing the URL drops the recorded export and deactivates a variant.

        Args:
            identifier: Library identifier of the upload.
            metadata: New metadata; its kind must match the stored kind.

        Raises:
            ValueError: If the upload is missing or its kind changed, or the metadata
                tries to record an export.
        """
        with private_file_lock(self._root / ".library.lock"):
            library = self._library()
            row = next(
                (row for row in library["entries"] if row["id"] == identifier), None
            )
            if row is None or row.get("kind", "legacy") != metadata.kind:
                raise ValueError("Reload the resume before editing its details")
            if metadata.provenance or metadata.exported_at:
                raise ValueError("Upload the exported PDF to record a new export")
            if metadata.google_doc_url != row.get("google_doc_url", ""):
                row["pdf_export"] = None
                if metadata.kind == "variant":
                    row["active"] = False
            row.update(
                label=metadata.label or row["original_name"],
                role=metadata.role,
                google_doc_url=metadata.google_doc_url,
            )
            self._publish_library(library)

    def delete(self, stored_name: str) -> None:
        """Delete one superseded upload only when it belongs to this store.

        Args:
            stored_name: Store-generated basename to remove.

        Raises:
            ValueError: If the name cannot resolve inside this store.
        """
        candidate = (self._root / Path(stored_name).name).resolve()
        if candidate.parent != self._root:
            raise ValueError("Invalid stored résumé name")
        candidate.unlink(missing_ok=True)


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    if any(character < " " for character in name):
        raise ValueError("Invalid resume filename")
    return name or "resume"


def _extract_text(extension: str, content: bytes) -> str:
    if extension in {".md", ".txt"}:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("The résumé must contain valid UTF-8 text") from exc
    if extension == ".docx":
        try:
            document = Document(io.BytesIO(content))
        except Exception as exc:
            raise ValueError("The DOCX résumé could not be read") from exc
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF résumés are not supported")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The PDF résumé could not be read") from exc


def _write_private_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["ResumeFileStore"]


@contextmanager
def private_file_lock(path: Path) -> Iterator[None]:
    """Serialize related private draft updates before atomic publication.

    Args:
        path: Lock file, created with mode 0600 if missing.

    Returns:
        A context manager that holds an exclusive lock while open.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        path.chmod(0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield
