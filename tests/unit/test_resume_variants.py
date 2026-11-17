"""Master ownership and the PDF export contract for application variants."""

import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader, PdfWriter

from jobfeed.resume_library_types import ResumeUploadMetadata
from tests.unit.test_onboarding_resume import _pdf_bytes
from tests.unit.test_resume_library import _services

DOC = 'https://docs.google.com/document/d/variant-document/edit'


def variant(**changes):
    return ResumeUploadMetadata(**{
        'kind':'variant', 'label':'Software Engineer', 'role':'SWE',
        'google_doc_url':DOC, 'provenance':'observed_google_docs_export',
        'exported_at':'2026-10-23T12:00:00+00:00', **changes,
    })


def test_master_is_singleton_never_selectable_and_retains_original_bytes(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('Master.pdf', _pdf_bytes('All projects'),
                      resume_metadata=ResumeUploadMetadata(kind='master', label='Master'))
    initial = files.library()[0]
    assert profile.state().application_resumes == []
    with pytest.raises(ValueError):
        profile.set_active(initial['id'], True)
    onboarding.upload('Complete-source.txt', b'All projects and all bullets',
                      resume_metadata=ResumeUploadMetadata(kind='master', label='Master'))
    state = profile.state()
    assert len(state.resumes) == 1
    assert state.resumes[0].id == initial['id']
    assert state.resumes[0].filename == 'Complete-source.txt'
    assert state.resumes[0].active is False
    assert state.application_resumes == []
    assert (files._root / initial['stored_name']).read_bytes() == _pdf_bytes('All projects')


def test_variant_records_export_without_replacing_scoring_master(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    master = onboarding.upload('Master.md', b'All original experience',
                               resume_metadata=ResumeUploadMetadata(kind='master'))
    returned = onboarding.upload('SWE.pdf', _pdf_bytes('Selected SWE experience'),
                                 resume_metadata=variant())
    assert returned == master == onboarding.state()
    state = profile.state()
    row = next(row for row in state.resumes if row.kind == 'variant')
    assert row.application_supported is True and row.active is True
    assert row.page_count == 1 and row.google_doc_url == DOC
    assert 'stored_name' not in row.pdf_export
    catalog = json.loads(profile.catalog_path.read_text())['resumes']
    assert len(catalog) == 1 and catalog[0]['role'] == 'SWE'
    assert catalog[0]['pdf_export']['stored_name'] == catalog[0]['profile_upload']
    path, filename = profile.download_resume(row.id)
    assert filename == 'SWE.pdf' and path.read_bytes() == _pdf_bytes('Selected SWE experience')
    assert len(files.library()) == 2


def test_invalid_variant_replacement_preserves_valid_export(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('SWE.pdf', _pdf_bytes('Selected experience'), resume_metadata=variant())
    before = files.library()
    profile.state()
    catalog_before = profile.catalog_path.read_bytes()
    writer = PdfWriter()
    page = PdfReader(io.BytesIO(_pdf_bytes('Two pages'))).pages[0]
    writer.add_page(page)
    writer.add_page(page)
    content = io.BytesIO()
    writer.write(content)
    with pytest.raises(ValueError, match='one-page'):
        onboarding.upload('SWE.pdf', content.getvalue(), resume_metadata=variant(), identifier=before[0]['id'])
    assert files.library() == before
    assert profile.catalog_path.read_bytes() == catalog_before


def test_source_change_clears_export_until_new_pdf_is_registered(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('SWE.pdf', _pdf_bytes('Experience'), resume_metadata=variant())
    identifier = files.library()[0]['id']
    metadata = variant(google_doc_url=DOC.replace('variant-document', 'new-document'),
                       provenance=None, exported_at=None)
    state = profile.update_resume(identifier, metadata)
    assert state.resumes[0].active is False
    assert state.resumes[0].pdf_export is None
    assert state.resumes[0].application_supported is False
    with pytest.raises(ValueError):
        profile.set_active(identifier, True)
    assert state.application_resumes[0].available is False


def test_cross_purpose_same_filename_cannot_replace_master(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('Resume.pdf', _pdf_bytes('Master'), resume_metadata=ResumeUploadMetadata(kind='master'))
    original = files.library()
    with pytest.raises(ValueError, match='different filename'):
        onboarding.upload('resume.PDF', _pdf_bytes('Variant'), resume_metadata=variant())
    with pytest.raises(ValueError, match='different filename'):
        onboarding.upload('Resume.pdf', _pdf_bytes('Legacy'))
    assert files.library() == original
    assert profile.state().application_resumes == []


def test_concurrent_master_replacements_keep_one_stable_id(tmp_path):
    files, _, _, _ = _services(tmp_path)
    files.save('Master.txt', b'Initial', resume_metadata=ResumeUploadMetadata(kind='master'))
    identifier = files.library()[0]['id']
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda name: files.save(name, name.encode(),
            resume_metadata=ResumeUploadMetadata(kind='master')), ['Master2.txt', 'Master3.txt']))
    assert len(files.library()) == 1
    assert files.library()[0]['id'] == identifier


def test_renamed_variant_keeps_one_catalog_entry(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('SWE.pdf', _pdf_bytes('First'), resume_metadata=variant())
    identifier = files.library()[0]['id']
    profile.state()
    onboarding.upload('SWE-updated.pdf', _pdf_bytes('Second'), resume_metadata=variant(), identifier=identifier)
    state = profile.state()
    assert len(state.application_resumes) == 1
    assert state.application_resumes[0].id == identifier
    assert state.application_resumes[0].filename == 'SWE-updated.pdf'


def test_new_variant_filename_collision_preserves_existing_export(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload('SWE.pdf', _pdf_bytes('Original selected experience'), resume_metadata=variant())
    identifier = files.library()[0]['id']
    profile.set_active(identifier, False)
    before = files.library()
    old_path = files._root / before[0]['stored_name']
    old_bytes = old_path.read_bytes()
    catalog = profile.catalog_path.read_bytes()
    with pytest.raises(ValueError, match='Edit or replace'):
        onboarding.upload('swe.PDF', _pdf_bytes('A different role'),
                          resume_metadata=variant(role='AI Engineer', google_doc_url=DOC.replace('variant-document', 'other-document')))
    assert files.library() == before
    assert old_path.read_bytes() == old_bytes
    assert profile.catalog_path.read_bytes() == catalog
    assert files.library()[0]['pdf_export'] == before[0]['pdf_export']
    assert files.library()[0]['active'] is False
    onboarding.upload('SWE.pdf', _pdf_bytes('Explicit replacement'),
                      resume_metadata=variant(), identifier=identifier)
    assert len(files.library()) == 1
    assert files.library()[0]['id'] == identifier
    assert files.library()[0]['active'] is False
