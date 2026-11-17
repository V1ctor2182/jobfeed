"""Personal profile edits and explicit application résumé catalog management."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from pypdf import PdfReader

from jobfeed.my_profile_document import PersonalProfileStore
from jobfeed.my_profile_models import ApplicationResume, MyProfileState, PersonalDetails, UploadedResume
from jobfeed.onboarding_resume_files import ResumeFileStore, _write_private_bytes, private_file_lock
from jobfeed.onboarding_resume_types import ResumeDraftState
from jobfeed.resume_library_types import ResumeUploadMetadata, variant_export_matches


class MyProfileService:
    """Keep application facts and existing application PDFs independent of workers."""

    def __init__(
        self,
        *,
        root: Path,
        pipeline_root: Path,
        resume_state: Callable[[], ResumeDraftState],
    ) -> None:
        self.root = root
        self.pipeline_root = pipeline_root
        self.resume_state = resume_state
        self.personal = PersonalProfileStore(
            pipeline_root / "application-profile.md",
            root / "data/my-profile-review.json",
        )
        self.catalog_path = pipeline_root / "resume-catalog.json"

    def _catalog(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            return {"schema_version": 1, "resumes": []}
        catalog = json.loads(self.catalog_path.read_text())
        if (
            not isinstance(catalog, dict)
            or catalog.get("schema_version") != 1
            or not isinstance(catalog.get("resumes"), list)
        ):
            raise ValueError(
                "The application résumé catalog needs repair before editing."
            )
        ids = set()
        for entry in catalog["resumes"]:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or entry["id"] in ids
            ):
                raise ValueError("The application résumé catalog has invalid entries.")
            ids.add(entry["id"])
        return catalog

    def catalog_ready(self) -> bool:
        """Check currently enabled source PDFs, independent of cached diagnostics.

        Returns:
            True when at least one catalog entry is enabled; every enabled PDF must
            also be readable, or reading it raises.
        """
        enabled = [
            entry
            for entry in self._catalog()["resumes"]
            if entry.get("available") is True
        ]
        for entry in enabled:
            source = Path(str(entry.get("pdf", "")))
            _pdf_bytes(source if source.is_absolute() else self.pipeline_root / source)
        return bool(enabled)

    def _known_names(self, catalog: dict[str, Any]) -> dict[str, str]:
        uploaded = self.resume_state()
        names = {}
        if uploaded.stored_name and uploaded.original_name:
            names[uploaded.stored_name] = uploaded.original_name
        for entry in catalog['resumes']:
            label = entry.get('original_filename', entry.get('label', ''))
            if entry.get('profile_upload') and Path(label).suffix.lower() in {'.pdf','.docx','.md','.txt'}:
                names.setdefault(entry['profile_upload'], label)
        return names

    def _filename(self, entry: dict, names: dict[str,str]) -> str:
        if entry.get('profile_upload') in names:
            return names[entry['profile_upload']]
        return Path(str(entry.get('pdf',''))).name

    def upload_metadata(self) -> tuple[dict[str,str],dict[str,bool]]:
        """Read existing filename and active choices without migrating files.

        Returns:
            Display names by stored filename, and active choices by casefolded
            display name.
        """
        catalog = self._catalog()
        names = self._known_names(catalog)
        active = {}
        for entry in catalog['resumes']:
            key = self._filename(entry,names).casefold()
            active[key] = active.get(key,True) and entry.get('available') is True
        return names,active

    def _sync_resumes(self) -> tuple[list[dict], dict]:
        """Reconcile the catalog with the upload library and publish any changes.

        Catalog entries backed by an uploaded PDF take that upload's path, metadata
        and availability; uploaded PDFs missing from the catalog are added, and
        masters are left out.

        Time complexity: O(C + U) for C catalog entries and U uploads; the inner
        loop only drops a fixed set of three fields.

        Returns:
            The upload library entries and the published catalog.
        """
        with private_file_lock(self.catalog_path.with_suffix('.lock')):
            catalog = self._catalog()
            names = self._known_names(catalog)
            files = ResumeFileStore(self.root / 'data/resumes')
            _,active = self.upload_metadata()
            uploaded = files.library(names,active)
            by_name = {row['original_name'].casefold():row for row in uploaded}
            by_id = {row['id']:row for row in uploaded}
            retained = []
            seen = set()
            for entry in catalog['resumes']:
                filename = self._filename(entry,names)
                key = filename.casefold()
                if key in seen:
                    continue
                seen.add(key)
                upload = by_id.get(entry['id']) or by_name.get(key)
                if upload:
                    seen.add(upload['original_name'].casefold())
                if upload and upload.get('kind') == 'master':
                    continue
                if upload and Path(upload['original_name']).suffix.lower() == '.pdf':
                    source = self.root / 'data/resumes' / upload['stored_name']
                    readable = _readable_pdf(source)
                    if readable:
                        source.chmod(0o600)
                    previous_source = Path(str(entry.get('pdf','')))
                    previous_source = previous_source if previous_source.is_absolute() else self.pipeline_root / previous_source
                    metadata = dict(entry)
                    if previous_source.resolve() != source.resolve():
                        for field in ('autofill','text','description'):
                            metadata.pop(field,None)
                    retained.append({**metadata,'id':upload['id'],'label':upload['original_name'],
                        'pdf':str(source.resolve()),'profile_upload':upload['stored_name'],
                        **_catalog_metadata(upload), 'available':upload['active'] and readable and _eligible(upload, source)})
                else:
                    retained.append(entry)
            for upload in uploaded:
                if upload['original_name'].casefold() in seen or Path(upload['original_name']).suffix.lower() != '.pdf' or upload.get('kind') == 'master':
                    continue
                source = self.root / 'data/resumes' / upload['stored_name']
                readable = _readable_pdf(source)
                if readable:
                    source.chmod(0o600)
                retained.append({'id':upload['id'],'label':upload['original_name'],'pdf':str(source.resolve()),
                    'profile_upload':upload['stored_name'], **_catalog_metadata(upload),
                    'available':upload['active'] and readable and _eligible(upload, source)})
            updated = {**catalog,'resumes':retained}
            if updated != catalog:
                self._publish_catalog(updated)
            return uploaded, updated

    def state(self) -> MyProfileState:
        """Return filename metadata and active status without private paths.

        Returns:
            Personal details, uploaded résumés and application catalog entries.
        """
        personal = self.personal.state()
        uploaded, catalog = self._sync_resumes()
        entries = []
        resumes = []
        names = self._known_names(catalog)
        upload_ids = {row['id'] for row in uploaded}
        for row in uploaded:
            source = self.root / 'data/resumes' / row['stored_name']
            supported = Path(row['original_name']).suffix.lower() == '.pdf'
            missing = not (_readable_pdf(source) if supported else source.is_file() and not source.is_symlink())
            kind = row.get('kind', 'legacy')
            eligible = supported and not missing and _eligible(row, source)
            status = 'File unavailable; upload again' if missing else (('Available for applications' if row['active'] else 'Inactive for future applications') if supported else 'Scoring only; applications require PDF')
            if kind == 'master' and not missing:
                status = 'Complete source material; never used as an application PDF'
            elif kind == 'variant' and not eligible and not missing:
                status = 'Upload a one-page PDF exported from this Google Doc to make it available'
            export = row.get('pdf_export')
            public_export = {key:value for key,value in export.items() if key != 'stored_name'} if export else None
            resumes.append(UploadedResume(id=row['id'],filename=row['original_name'],active=row['active'],
                format=Path(row['original_name']).suffix.lower().lstrip('.'), application_supported=eligible,
                status=status, kind=kind, label=row.get('label', row['original_name']),
                role=row.get('role', ''), google_doc_url=row.get('google_doc_url', ''),
                page_count=row.get('page_count'), pdf_export=public_export,
                download_url=f'/api/my-profile/resumes/{row["id"]}/download' if not missing else ''))
        for entry in catalog['resumes']:
            filename = self._filename(entry,names)
            entries.append(ApplicationResume(id=entry['id'],label=str(entry.get('label',filename)),
                filename=filename,available=entry.get('available') is True))
            if entry['id'] not in upload_ids:
                source = Path(str(entry.get('pdf','')))
                source = source if source.is_absolute() else self.pipeline_root / source
                exists = source.is_file() and not source.is_symlink()
                resumes.append(UploadedResume(id=entry['id'],filename=filename,active=entry.get('available') is True,
                    format=source.suffix.lower().lstrip('.'),application_supported=exists,
                    download_url=f'/api/my-profile/resumes/{entry["id"]}/download' if exists else '',
                    status=('Available for applications' if entry.get('available') is True else 'Inactive for future applications') if exists else 'File unavailable; upload again'))
        return MyProfileState(**personal.model_dump(),application_resumes=entries,resumes=resumes)

    def save_personal(self, details: PersonalDetails, revision: str) -> MyProfileState:
        """Publish personal facts without changing any application authority.

        Args:
            details: Personal details to write.
            revision: Profile revision the edit was based on.

        Returns:
            The updated My Profile state.
        """
        self._catalog()
        self.personal.save(details, revision)
        return self.state()

    def _publish_catalog(self, catalog: dict[str, Any]) -> None:
        _write_private_bytes(
            self.catalog_path,
            (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode(),
        )

    def add_current_resume(self) -> MyProfileState:
        """Compatibility entry point; PDF uploads are automatically catalogued.

        Returns:
            The current My Profile state once the upload is confirmed as a PDF.

        Raises:
            ValueError: If there is no current upload or it is not a PDF.
        """
        uploaded = self.resume_state()
        if not uploaded.stored_name or Path(uploaded.stored_name).name != uploaded.stored_name:
            raise ValueError('Upload a PDF résumé first.')
        source = self.root / 'data/resumes' / uploaded.stored_name
        if source.suffix.lower() != '.pdf':
            raise ValueError('Applications require an existing PDF. Upload a PDF résumé first.')
        _pdf_bytes(source)
        return self.state()

    def set_active(self, identifier: str, active: bool) -> MyProfileState:
        """Enable or disable one filename for future work, preserving old bytes.

        Args:
            identifier: Upload library or catalog identifier of the résumé.
            active: Whether the résumé should be used for future work.

        Returns:
            The updated My Profile state.

        Raises:
            ValueError: If an uploaded PDF being enabled lacks a valid one-page
                Google Docs export, or the identifier is unknown.
        """
        uploaded, catalog = self._sync_resumes()
        row = next((row for row in uploaded if row['id'] == identifier), None)
        if row is not None:
            stored = self.root / 'data/resumes' / row['stored_name']
            pdf = active and Path(row['original_name']).suffix.lower() == '.pdf'
            if pdf:
                _pdf_bytes(stored)
            if pdf and not _eligible(row, stored):
                raise ValueError('This resume needs a valid one-page Google Docs export')
            ResumeFileStore(self.root / 'data/resumes').set_active(identifier,active,self._known_names(catalog))
            return self.state()
        with private_file_lock(self.catalog_path.with_suffix('.lock')):
            catalog = self._catalog()
            entry = next((row for row in catalog['resumes'] if row['id'] == identifier),None)
            if entry is None:
                raise ValueError('This resume no longer exists. Reload My Profile.')
            if active:
                source = Path(str(entry.get('pdf','')))
                _pdf_bytes(source if source.is_absolute() else self.pipeline_root / source)
            entry['available'] = active
            self._publish_catalog(catalog)
        return self.state()

    def set_available(self, identifier: str, available: bool) -> MyProfileState:
        """Alias of `set_active` for the application catalog route.

        Args:
            identifier: Upload library or catalog identifier of the résumé.
            available: Whether the résumé should be used for future work.

        Returns:
            The updated My Profile state.
        """
        return self.set_active(identifier,available)

    def update_resume(self, identifier: str, metadata: ResumeUploadMetadata) -> MyProfileState:
        """Update one uploaded résumé's metadata.

        Args:
            identifier: Library identifier of the upload.
            metadata: New label, role and Google Docs URL.

        Returns:
            The updated My Profile state.
        """
        ResumeFileStore(self.root / 'data/resumes').update_metadata(identifier, metadata)
        return self.state()

    def download_resume(self, identifier: str) -> tuple[Path, str]:
        """Locate a résumé file to download, from the uploads or the catalog.

        Args:
            identifier: Upload library or catalog identifier of the résumé.

        Returns:
            The file's path and the filename to download it as.

        Raises:
            ValueError: If the identifier is unknown, or the file is missing or a
                symbolic link.
        """
        uploaded, catalog = self._sync_resumes()
        row = next((row for row in uploaded if row['id'] == identifier), None)
        if row:
            source = self.root / 'data/resumes' / row['stored_name']
            filename = row['original_name']
        else:
            entry = next((entry for entry in catalog['resumes'] if entry['id'] == identifier), None)
            if entry is None:
                raise ValueError('This resume no longer exists')
            source = Path(str(entry['pdf']))
            source = source if source.is_absolute() else self.pipeline_root / source
            filename = self._filename(entry, self._known_names(catalog))
        if source.is_symlink() or not source.is_file():
            raise ValueError('The resume file is unavailable')
        return source, filename


def _catalog_metadata(row: dict) -> dict:
    if row.get('kind', 'legacy') == 'legacy':
        return {}
    return {'original_filename':row['original_name'], **{key:row.get(key) for key in ('kind', 'label', 'role', 'google_doc_url', 'page_count', 'pdf_export')}}


def _eligible(row: dict, source: Path) -> bool:
    if row.get('kind') == 'master':
        return False
    if row.get('kind', 'legacy') == 'legacy':
        return True
    if not variant_export_matches(row):
        return False
    try:
        reader = PdfReader(source)
        return not reader.is_encrypted and len(reader.pages) == 1 and bool((reader.pages[0].extract_text() or '').strip())
    except Exception:
        return False


def _readable_pdf(path: Path) -> bool:
    try:
        _pdf_bytes(path)
        return True
    except (ValueError,OSError):
        return False


def _pdf_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("The selected PDF is unavailable. Upload it again.")
    content = path.read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ValueError("The selected file is not a valid PDF.")
    return content
