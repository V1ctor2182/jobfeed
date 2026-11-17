"""One guarded upload updates the filename library and future PDF catalog."""
from pathlib import Path
import json

import httpx
from fastapi import FastAPI

from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.my_profile import router as profile_router
from jobfeed.web.routes.onboarding_resume import router as resume_router
from tests.unit.test_onboarding_resume import _pdf_bytes
from tests.unit.test_resume_library import _services

ORIGIN='http://127.0.0.1:8765'


def _app(tmp_path):
    _,_,onboarding,profile=_services(tmp_path)
    app=FastAPI()
    install_error_handling(app)
    app.state.onboarding_resume_service=onboarding
    app.state.my_profile_service=profile
    app.include_router(profile_router,prefix='/api')
    app.include_router(resume_router,prefix='/api')
    return app,profile


async def test_one_upload_is_active_and_toggle_survives_same_name_replacement(tmp_path:Path):
    app,profile=_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers={'Origin':ORIGIN}) as client:
        response=await client.post('/api/onboarding/resume',files={'file':('Resume.pdf',_pdf_bytes('Initial'),'application/pdf')})
        assert response.status_code==200
        state=(await client.get('/api/my-profile')).json()
        assert len(state['resumes'])==1
        row=state['resumes'][0]
        assert row['filename']=='Resume.pdf' and row['active'] is True
        assert row['application_supported'] is True
        identifier=row['id']
        response=await client.put(f'/api/my-profile/resumes/{identifier}',json={'active':False})
        assert response.status_code==200
        response=await client.post('/api/onboarding/resume',files={'file':('Resume.pdf',_pdf_bytes('Replacement'),'application/pdf')})
        assert response.status_code==200
        state=(await client.get('/api/my-profile')).json()
        assert len(state['resumes'])==1
        assert state['resumes'][0]['id']==identifier
        assert state['resumes'][0]['active'] is False
        assert state['application_resumes'][0]['available'] is False
        assert str(tmp_path) not in response.text


async def test_cross_site_upload_has_no_file_or_catalog_mutation(tmp_path:Path):
    app,profile=_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
        response=await client.post('/api/onboarding/resume',headers={'Origin':'https://example.test'},files={'file':('Resume.pdf',_pdf_bytes('Forbidden'),'application/pdf')})
    assert response.status_code==403
    assert not (profile.root/'data/resumes').exists()
    assert not profile.catalog_path.exists()


async def test_purpose_upload_download_and_source_edit(tmp_path:Path):
    app,profile=_app(tmp_path)
    metadata={'kind':'variant','label':'SWE','role':'Software Engineer',
              'google_doc_url':'https://docs.google.com/document/d/swe/edit','provenance':'user_declared'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers={'Origin':ORIGIN}) as client:
        response=await client.post('/api/my-profile/resumes/upload',data={'metadata':json.dumps(metadata)},
                                  files={'file':('SWE.pdf',_pdf_bytes('SWE experience'),'application/pdf')})
        assert response.status_code==200
        row=response.json()['resumes'][0]
        assert row['kind']=='variant' and row['page_count']==1 and row['active'] is True
        assert str(tmp_path) not in response.text
        downloaded=await client.get(row['download_url'])
        assert downloaded.content==_pdf_bytes('SWE experience')
        assert 'SWE.pdf' in downloaded.headers['content-disposition']
        collision=await client.post('/api/my-profile/resumes/upload',data={'metadata':json.dumps(metadata)},
                                   files={'file':('SWE.pdf',_pdf_bytes('Different content'),'application/pdf')})
        assert collision.status_code==422
        assert 'Edit or replace' in collision.text
        assert (await client.get(row['download_url'])).content==downloaded.content
        metadata['google_doc_url']='https://docs.google.com/document/d/changed/edit'
        metadata['provenance']=None
        response=await client.put(f'/api/my-profile/resumes/{row["id"]}/details',json=metadata)
        assert response.status_code==200
        assert response.json()['resumes'][0]['active'] is False
        assert response.json()['resumes'][0]['pdf_export'] is None
        forbidden=await client.post('/api/my-profile/resumes/upload',headers={'Origin':'https://example.test'},
            data={'metadata':json.dumps(metadata)},files={'file':('Other.pdf',_pdf_bytes('Other'),'application/pdf')})
        assert forbidden.status_code==403
    assert len(profile.state().resumes)==1
