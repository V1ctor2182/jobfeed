"""Source metadata survives the existing frozen-resume contract."""

import json

import pytest

from pipeline.resume_catalog import ResumeCatalog


def test_master_excluded_and_variant_source_preserved_in_frozen_catalog(tmp_path):
    source = tmp_path / 'export.pdf'
    source.write_bytes(b'%PDF-1.4 existing exported bytes')
    export = {'stored_name':'export.pdf', 'source_google_doc_url':'https://docs.google.com/document/d/swe/edit',
              'page_count':1, 'exported_at':None, 'provenance':'user_declared'}
    variant = {'id':'swe', 'label':'SWE', 'kind':'variant', 'role':'Software Engineer',
               'google_doc_url':export['source_google_doc_url'], 'page_count':1,
               'pdf_export':export, 'profile_upload':'export.pdf', 'pdf':str(source), 'available':True}
    catalog = tmp_path / 'resume-catalog.json'
    catalog.write_text(json.dumps({'schema_version':1, 'resumes':[
        {'id':'master', 'label':'Master', 'kind':'master', 'available':True, 'pdf':str(source)}, variant,
    ]}))
    frozen = ResumeCatalog.load(catalog).freeze(tmp_path)
    assert len(frozen['entries']) == 1
    entry = frozen['entries'][0]
    assert entry['id'] == 'swe'
    assert entry['role'] == 'Software Engineer'
    assert entry['google_doc_url'] == export['source_google_doc_url']
    assert entry['pdf_export'] == export
    assert (tmp_path / entry['assets']['pdf']['path']).read_bytes() == source.read_bytes()
    variant['google_doc_url'] = 'https://docs.google.com/document/d/different/edit'
    catalog.write_text(json.dumps({'schema_version':1, 'resumes':[variant]}))
    with pytest.raises(ValueError, match='recorded one-page'):
        ResumeCatalog.load(catalog)
